#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import time
from pathlib import Path

from psycopg.types.json import Jsonb

from common import ROOT, connect, ensure_ops_schema


def output_paths(pdf_path: Path, pdf_root: Path, output_root: Path) -> tuple[Path, Path, Path]:
    stem = output_root / safe_stem(pdf_path.stem)
    return stem.with_suffix(".md"), stem.with_name(stem.name + "_quality.json"), stem.with_name(stem.name + "_quality.md")


def safe_stem(stem: str, max_bytes: int = 150) -> str:
    if len(stem.encode("utf-8")) <= max_bytes:
        return stem
    digest = hashlib.sha1(stem.encode("utf-8", errors="replace")).hexdigest()[:12]
    suffix = "_" + digest
    budget = max_bytes - len(suffix.encode("utf-8"))
    encoded = stem.encode("utf-8", errors="replace")[:budget]
    while encoded:
        try:
            return encoded.decode("utf-8").rstrip("._- ") + suffix
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return digest


def discover_one(report_type: str, pdf_root: Path, output_root: Path, max_age_new_seconds: int) -> list[dict]:
    now = time.time()
    rows = []
    seen = set()
    for pdf_path in sorted(pdf_root.rglob("*.pdf")):
        pdf_path = pdf_path.resolve()
        if pdf_path in seen:
            continue
        seen.add(pdf_path)
        md_path, quality_json_path, quality_md_path = output_paths(pdf_path, pdf_root, output_root)
        missing = [
            name
            for name, path in (
                ("md", md_path),
                ("quality_json", quality_json_path),
                ("quality_md", quality_md_path),
            )
            if not path.exists() or path.stat().st_size == 0
        ]
        if not missing:
            continue
        age = now - pdf_path.stat().st_mtime
        priority = 10 if age <= max_age_new_seconds else 100
        rows.append(
            {
                "report_type": report_type,
                "pdf_path": str(pdf_path),
                "md_path": str(md_path.resolve()),
                "quality_json_path": str(quality_json_path.resolve()),
                "quality_md_path": str(quality_md_path.resolve()),
                "priority": priority,
                "metadata": {"missing": missing, "pdf_mtime": pdf_path.stat().st_mtime,
                             "input_signature": f"{pdf_path.stat().st_size}:{pdf_path.stat().st_mtime_ns}"},
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover Eastmoney report PDFs missing OCR outputs and enqueue them.")
    parser.add_argument("--max-age-new-seconds", type=int, default=2 * 24 * 3600)
    args = parser.parse_args()

    ensure_ops_schema()
    rows = []
    rows.extend(discover_one("stock", ROOT / "PDF", ROOT / "PDF_md", args.max_age_new_seconds))
    rows.extend(discover_one("industry", ROOT / "PDF_ind", ROOT / "PDF_ind_md", args.max_age_new_seconds))

    inserted = 0
    with connect(admin=False) as conn:
        for row in rows:
            result = conn.execute(
                """
                INSERT INTO ops.ocr_queue (
                  source_name, report_type, pdf_path, md_path, quality_json_path,
                  quality_md_path, status, priority, metadata, updated_at
                )
                VALUES ('eastmoney', %s, %s, %s, %s, %s, 'pending', %s, %s, now())
                ON CONFLICT (pdf_path) DO UPDATE
                SET md_path = EXCLUDED.md_path,
                    quality_json_path = EXCLUDED.quality_json_path,
                    quality_md_path = EXCLUDED.quality_md_path,
                    priority = LEAST(ops.ocr_queue.priority, EXCLUDED.priority),
                    status = CASE
                      WHEN ops.ocr_queue.status = 'running' THEN 'running'
                      WHEN ops.ocr_queue.metadata->>'input_signature' IS NOT NULL
                       AND ops.ocr_queue.metadata->>'input_signature' IS DISTINCT FROM EXCLUDED.metadata->>'input_signature' THEN 'pending'
                      WHEN ops.ocr_queue.status IN ('failed', 'skipped') THEN ops.ocr_queue.status
                      ELSE 'pending'
                    END,
                    retry_count = CASE
                      WHEN ops.ocr_queue.status != 'running'
                       AND ops.ocr_queue.metadata->>'input_signature' IS NOT NULL
                       AND ops.ocr_queue.metadata->>'input_signature' IS DISTINCT FROM EXCLUDED.metadata->>'input_signature' THEN 0
                      ELSE ops.ocr_queue.retry_count END,
                    metadata = ops.ocr_queue.metadata || EXCLUDED.metadata,
                    updated_at = now()
                RETURNING id
                """,
                (
                    row["report_type"],
                    row["pdf_path"],
                    row["md_path"],
                    row["quality_json_path"],
                    row["quality_md_path"],
                    row["priority"],
                    Jsonb(row["metadata"]),
                ),
            ).fetchone()
            if result:
                inserted += 1
    print(f"[ocr_discover] candidates={len(rows)} upserted={inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

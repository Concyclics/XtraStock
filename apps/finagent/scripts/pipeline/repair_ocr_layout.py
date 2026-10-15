#!/usr/bin/env python3
"""Resume figure OCR for outputs affected by wrapped DeepSeek layout labels.

Writes to a separate directory. Existing page OCR is reused; figure results
are checkpointed per page. Failed figures prevent publication of a final file.
"""
from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import argparse
from dataclasses import asdict
import json
from pathlib import Path
import re
import sys
import time

from common import ROOT, load_env
sys.path.insert(0, str(ROOT))
import pdf_ocr_markdown as ocr
from report_attachments import atomic_write, output_paths, sha256
import fitz
import requests


def repair(source: Path, input_md: Path, output_dir: Path, figure_review: Path | None = None) -> dict:
    source_hash = sha256(source.read_bytes())
    review = json.loads(figure_review.read_text()) if figure_review else {}
    if review and review.get("source_sha256") != source_hash:
        raise ValueError("figure review belongs to a different source file")
    exclusions = {(x["page"], tuple(x["box"])): x["reason"] for x in review.get("excluded_boxes", [])}
    original = input_md.read_text()
    blocks = [(int(m.group(1)), m.group(2).strip()) for m in re.finditer(
        r"<!-- page (\d+) -->(.*?)(?=<!-- page \d+ -->|\Z)", original, re.S)]
    if not blocks or len({p for p, _ in blocks}) != len(blocks):
        raise ValueError("missing or duplicate page markers")
    output_dir.mkdir(parents=True, exist_ok=True)
    cache = output_dir / "page-checkpoints"
    cache.mkdir(exist_ok=True)
    session = requests.Session()
    session.headers.update(ocr.api_headers())
    import os
    base = os.getenv("FINANCE_OCR_BASE_URL", os.getenv("FINANCE_API_BASE_URL", ""))
    from urllib.parse import urlparse
    if urlparse(base).hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("this repair only uses the local OCR endpoint")
    model = os.getenv("FINANCE_OCR_MODEL") or ocr.get_first_model(base, 180)
    results = []
    native_short_pages = []
    with fitz.open(source) as doc:
        for number, raw in blocks:
            key = sha256((source_hash + raw + "wrapped-layout-v2" + json.dumps(review, sort_keys=True)).encode())
            checkpoint = cache / f"page-{number:04d}.json"
            saved = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
            if saved.get("key") == key:
                result = saved["result"]
                result["quality"] = ocr.PageQuality(**result["quality"])
                results.append(ocr.PageResult(**result))
                continue
            started = time.monotonic()
            page = doc[number - 1]
            text = ocr.clean_markdown(raw, False)
            boxes = ocr.extract_layout_boxes(raw, {"image", "figure"})
            figures = []
            figure_records = []
            if boxes:
                rendered = ocr.render_page_to_image(page, 144, 1800)
                for _, box in boxes:
                    if (number, box) in exclusions:
                        figure_records.append({"box": box, "method": "reviewed_decoration",
                                               "reason": exclusions[(number, box)]})
                        continue
                    native = ocr.native_vector_figure_text(page, box)
                    if native:
                        figures.append(native)
                        figure_records.append({"box": box, "method": "native_vector_text"})
                        continue
                    if ocr.is_small_figure(rendered, box):
                        figure_records.append({"box": box, "method": "small_icon"})
                        continue
                    crop = ocr.crop_normalized_box(rendered, box, 35)
                    figure, finish, _ = ocr.call_ocr(session=session, base_url=base, model=model,
                        image_bytes=ocr.image_to_jpeg(crop, 88), prompt=ocr.DEFAULT_FIGURE_PROMPT,
                        max_tokens=4096, temperature=0, timeout=180, retries=2)
                    if finish not in {None, "stop", "eos"}:
                        failures = output_dir / "failed-figures"
                        failures.mkdir(exist_ok=True)
                        atomic_write(failures / f"page-{number:04d}-{'-'.join(map(str, box))}.json",
                            json.dumps({"page": number, "box": box, "finish_reason": finish, "output": figure}, ensure_ascii=False).encode())
                        raise RuntimeError(f"page {number} figure {box} did not complete: {finish}")
                    figures.append(ocr.clean_markdown(figure, False))
                    figure_records.append({"box": box, "method": "ocr", "finish_reason": finish})
                text = ocr.append_figure_markdown(text, figures)
            # Embedded text is independently retained where chart OCR/order
            # changes make the similarity score inconclusive. Never discard
            # original labels/dates/numbers on the basis of a model response.
            embedded = page.get_text("text") or ""
            quality = ocr.assess_quality(page_number=number, markdown=text, raw_markdown=raw,
                embedded_text=embedded, finish_reason="stop", elapsed_sec=time.monotonic()-started)
            if "low_similarity_to_embedded_text" in quality.warnings:
                text += "\n\n### 原文文字层\n\n" + embedded.strip()
                quality.chars = len(text)
                quality.cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
            result = ocr.PageResult(page=number, markdown=text, raw_markdown=raw, quality=quality, usage=None)
            atomic_write(checkpoint, json.dumps({"key": key, "figure_boxes": len(boxes), "figures_processed": len(figures), "figure_records": figure_records, "result": asdict(result)}, ensure_ascii=False).encode())
            results.append(result)
            print(json.dumps({"page": number, "figures": len(figures), "chars": len(text)}, ensure_ascii=False), flush=True)
        # Chapter dividers and page-number-only pages can legitimately produce
        # little/no OCR. Preserve their real text layer rather than leaving an
        # empty marker or declaring the native words lost.
        for result in results:
            if not set(result.quality.warnings) & {"short_output", "cleaning_removed_all_text"}:
                continue
            embedded = doc[result.page - 1].get_text("text") or ""
            if not embedded.strip():
                continue
            native_short_pages.append({"page": result.page, "original_warnings": result.quality.warnings})
            result.markdown = (result.markdown + "\n\n### 原文文字层\n\n" + embedded.strip()).strip()
            result.quality = ocr.assess_quality(page_number=result.page, markdown=result.markdown,
                raw_markdown=result.raw_markdown, embedded_text=embedded, finish_reason="stop",
                elapsed_sec=result.quality.elapsed_sec)
    if sha256(source.read_bytes()) != source_hash:
        raise RuntimeError("source changed during figure repair")
    md, q, qm = output_paths(source, output_dir)
    body = ocr.markdown_document(source, results)
    summary = ocr.quality_summary(source, results)
    summary.update(source_sha256=source_hash, content_sha256=sha256(body.encode()),
        extraction_method="pdf_ocr", attachment_format="pdf", search_full_body=True,
        parser_version="wrapped-layout-v2", layout_markers_removed=True,
        embedded_text_retained_on_low_similarity=True, manual_figure_review=review,
        short_pages_native_text_restored=native_short_pages)
    atomic_write(md, body.encode())
    atomic_write(q, (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode())
    ocr.write_quality_markdown(qm, summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("input_md", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--figure-review", type=Path, help="Source-hashed manual decorative-image review")
    args = parser.parse_args()
    import os
    os.environ.update(load_env(ROOT / ".env.finance-agent-v3"))
    summary = repair(args.source, args.input_md, args.output_dir, args.figure_review)
    print(json.dumps({"pages": summary["pages"], "failed_pages": summary["failed_pages"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

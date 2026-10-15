#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import sys
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/pipeline"))

from common import connect  # noqa: E402
from v3_ops import record_quality, set_dataset_watermark, stage_run  # noqa: E402


TEMPLATE_TITLES = {
    "行情中心", "东方财富网", "财经频道", "个股频道", "数据中心",
    "404", "页面不存在", "访问出错", "暂无内容",
}


def compact(value: Any) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())


def normalize_title(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).lower()


def score_row(row: dict[str, Any]) -> dict[str, Any]:
    title = compact(row.get("title"))
    body = compact(row.get("body"))
    body_chars = len(body)
    normalized_title = normalize_title(title)
    title_bad = not title or normalized_title in {normalize_title(v) for v in TEMPLATE_TITLES}
    template_noise = title_bad or any(
        marker in title for marker in ("页面不存在", "访问出错", "行情中心")
    )
    if not title and not body:
        status = "parse_failed"
        score = 0.0
    elif template_noise:
        status = "template_noise"
        score = 0.05
    elif body_chars < 100:
        status = "short"
        score = 0.35 if body_chars >= 20 else 0.2
    else:
        status = "accepted"
        score = min(1.0, 0.6 + min(body_chars, 5000) / 12500)
    return {
        "document_id": row["id"],
        "quality_status": status,
        "quality_score": score,
        "title_quality_score": 0.0 if title_bad else 1.0,
        "body_quality_score": min(1.0, body_chars / 1000) if body_chars else 0.0,
        "template_similarity": 1.0 if template_noise else 0.0,
        "body_chars": body_chars,
        "parser_version": "document_quality:v3.1",
        "previous_quality_status": row.get("previous_quality_status"),
        "is_indexed": bool(row.get("is_indexed")),
        "checks": {
            "normalized_title": normalized_title[:200],
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest() if body else None,
            "source_type": row.get("source_type"),
            "source_name": row.get("source_name"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Score and quarantine low-quality finance documents.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--recheck", action="store_true")
    args = parser.parse_args()
    batch_size = max(10, min(args.batch_size, 5000))
    processed = 0
    cursor_created_at: dt.datetime | None = None
    cursor_id = None

    with stage_run("finance_v3_search", "document_quality") as run:
        while not args.limit or processed < args.limit:
            take = min(batch_size, args.limit - processed) if args.limit else batch_size
            with connect(admin=False) as conn:
                rows = conn.execute(
                    """
                    SELECT d.id,d.source_type,d.source_name,d.title,d.created_at,
                           q.quality_status AS previous_quality_status,
                           EXISTS (
                             SELECT 1 FROM corpus.document_search_index s
                             WHERE s.document_id=d.id
                           ) AS is_indexed,
                           coalesce(d.content_text,d.content_md,d.raw_text,'') AS body
                    FROM corpus.documents d
                    LEFT JOIN corpus.document_quality q ON q.document_id=d.id
                    WHERE (%(recheck)s OR q.document_id IS NULL OR q.content_version < 1)
                      AND (
                        %(cursor_created_at)s::timestamptz IS NULL
                        OR (d.created_at,d.id) > (%(cursor_created_at)s::timestamptz,%(cursor_id)s::uuid)
                      )
                    ORDER BY d.created_at,d.id
                    LIMIT %(limit)s
                    """,
                    {
                        "recheck": args.recheck,
                        "cursor_created_at": cursor_created_at,
                        "cursor_id": cursor_id,
                        "limit": take,
                    },
                ).fetchall()
            if not rows:
                break
            scored = [score_row(dict(row)) for row in rows]
            with connect(admin=False) as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO corpus.document_quality (
                          document_id,quality_status,quality_score,title_quality_score,
                          body_quality_score,template_similarity,body_chars,parser_version,
                          content_version,checks,checked_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,%s,now())
                        ON CONFLICT (document_id) DO UPDATE SET
                          quality_status=EXCLUDED.quality_status,
                          duplicate_of=NULL,
                          quality_score=EXCLUDED.quality_score,
                          title_quality_score=EXCLUDED.title_quality_score,
                          body_quality_score=EXCLUDED.body_quality_score,
                          template_similarity=EXCLUDED.template_similarity,
                          body_chars=EXCLUDED.body_chars,
                          parser_version=EXCLUDED.parser_version,
                          content_version=EXCLUDED.content_version,
                          checks=EXCLUDED.checks,checked_at=now()
                        """,
                        [(
                            item["document_id"],item["quality_status"],item["quality_score"],
                            item["title_quality_score"],item["body_quality_score"],
                            item["template_similarity"],item["body_chars"],item["parser_version"],
                            Jsonb(item["checks"]),
                        ) for item in scored],
                    )
                    for item in scored:
                        bad = item["quality_status"] in {"template_noise", "parse_failed"}
                        previous_bad = item["previous_quality_status"] in {
                            "template_noise", "parse_failed", "quarantined", "duplicate"
                        }
                        if bad:
                            cur.execute(
                                """
                                INSERT INTO corpus.document_quarantine (
                                  document_id,reason_code,reason_detail,metadata
                                ) VALUES (%s,%s,%s,%s)
                                ON CONFLICT (document_id,reason_code,released_at)
                                DO UPDATE SET reason_detail=EXCLUDED.reason_detail,
                                              metadata=corpus.document_quarantine.metadata || EXCLUDED.metadata
                                """,
                                (
                                    item["document_id"],item["quality_status"],
                                    "automatic document quality gate",
                                    Jsonb({"parser_version": item["parser_version"]}),
                                ),
                            )
                            if item["is_indexed"] or item["previous_quality_status"] != item["quality_status"]:
                                cur.execute(
                                    "SELECT ops.enqueue_document_delete(%s,%s,20)",
                                    (item["document_id"], f"quality:{item['quality_status']}"),
                                )
                        else:
                            cur.execute(
                                """
                                UPDATE corpus.document_quarantine
                                SET released_at=now(),released_by='document_quality:v3.1'
                                WHERE document_id=%s AND released_at IS NULL
                                """,
                                (item["document_id"],),
                            )
                            if not item["is_indexed"] or previous_bad:
                                cur.execute(
                                    "SELECT ops.enqueue_document_index(%s,%s,80)",
                                    (item["document_id"], "quality:accepted_or_short"),
                                )
            processed += len(rows)
            cursor_created_at = rows[-1]["created_at"]
            cursor_id = rows[-1]["id"]
            print(f"document_quality rows={len(rows)} total={processed}")

        with connect(admin=False) as conn:
            # Generic annual-report titles are shared across issuers. Official
            # CNINFO filings may only deduplicate within one announcement ID.
            # Other repeated titles (e.g. 图片报道) require identical nonempty bodies.
            duplicate_row = conn.execute(
                """
                WITH ranked AS (
                  SELECT d.id,
                         first_value(d.id) OVER (
                           PARTITION BY d.source_type,d.source_name,
                             CASE WHEN d.source_name='cninfo'
                               THEN coalesce(nullif(d.external_id,''),nullif(d.url,''),d.id::text)
                               ELSE coalesce(md5(nullif(regexp_replace(coalesce(nullif(d.content_text,''),
                                 nullif(d.content_md,''),d.raw_text,''),'[[:space:]]+','','g'),'')),d.id::text) END,
                             (d.published_at AT TIME ZONE 'Asia/Shanghai')::date,
                             lower(regexp_replace(coalesce(d.title,''),'[^[:alnum:]一-龥]+','','g'))
                           ORDER BY d.created_at,d.id
                         ) AS keep_id,
                         row_number() OVER (
                           PARTITION BY d.source_type,d.source_name,
                             CASE WHEN d.source_name='cninfo'
                               THEN coalesce(nullif(d.external_id,''),nullif(d.url,''),d.id::text)
                               ELSE coalesce(md5(nullif(regexp_replace(coalesce(nullif(d.content_text,''),
                                 nullif(d.content_md,''),d.raw_text,''),'[[:space:]]+','','g'),'')),d.id::text) END,
                             (d.published_at AT TIME ZONE 'Asia/Shanghai')::date,
                             lower(regexp_replace(coalesce(d.title,''),'[^[:alnum:]一-龥]+','','g'))
                           ORDER BY d.created_at,d.id
                         ) AS rn
                  FROM corpus.documents d
                  WHERE coalesce(d.title,'') <> '' AND d.published_at IS NOT NULL
                ), changed AS (
                  UPDATE corpus.document_quality q
                  SET quality_status='duplicate',duplicate_of=r.keep_id,
                      quality_score=least(q.quality_score,0.25),checked_at=now()
                  FROM ranked r
                  WHERE q.document_id=r.id AND r.rn>1
                    AND q.quality_status NOT IN ('template_noise','parse_failed','quarantined')
                  RETURNING q.document_id
                ), enqueued AS (
                  SELECT ops.enqueue_document_delete(document_id,'quality:duplicate',30)
                  FROM changed
                )
                SELECT count(*) AS rows,(SELECT count(*) FROM enqueued) AS enqueued FROM changed
                """
            ).fetchone()
            summary = conn.execute(
                """
                SELECT count(*) AS rows,
                       (SELECT count(*) FROM corpus.documents) AS eligible_documents,
                       count(*) FILTER (WHERE quality_status='accepted') AS accepted,
                       count(*) FILTER (WHERE quality_status='short') AS short,
                       count(*) FILTER (WHERE quality_status='duplicate') AS duplicate,
                       count(*) FILTER (WHERE quality_status='template_noise') AS template_noise,
                       count(*) FILTER (WHERE quality_status='parse_failed') AS parse_failed,
                       max(checked_at) AS data_through
                FROM corpus.document_quality
                """
            ).fetchone()

        total = int(summary["rows"] or 0)
        eligible = int(summary["eligible_documents"] or 0)
        coverage_pct = 100.0 * total / eligible if eligible else 0.0
        bad = int(summary["template_noise"] or 0) + int(summary["parse_failed"] or 0)
        noise_pct = 100.0 * bad / total if total else 100.0
        duplicate_pct = 100.0 * int(summary["duplicate"] or 0) / total if total else 100.0
        quality = "fail" if coverage_pct < 99.0 else (
            "pass" if noise_pct < 1.0 and duplicate_pct < 0.5 else "warn"
        )
        record_quality(
            run["run_id"], "corpus.document_quality", "document_coverage",
            status="pass" if coverage_pct >= 99.0 else "fail", severity="error",
            observed_value=coverage_pct,threshold_value=99.0,
            affected_rows=max(eligible-total,0),
        )
        record_quality(
            run["run_id"], "corpus.document_quality", "template_and_parse_noise",
            status="pass" if noise_pct < 1.0 else "warn", severity="warning",
            observed_value=noise_pct,threshold_value=1.0,affected_rows=bad,
        )
        record_quality(
            run["run_id"], "corpus.document_quality", "duplicate_rate",
            status="pass" if duplicate_pct < 0.5 else "warn", severity="warning",
            observed_value=duplicate_pct,threshold_value=0.5,
            affected_rows=int(summary["duplicate"] or 0),
        )
        set_dataset_watermark(
            "corpus.document_quality",schema_version="v3",run_id=run["run_id"],
            expected_date=dt.date.today(),data_through=summary["data_through"],
            row_count=total,eligible_count=eligible,
            coverage_pct=coverage_pct,quality_status=quality,
            source_version="document_quality:v3.1",
            details={
                "accepted": int(summary["accepted"] or 0),
                "short": int(summary["short"] or 0),
                "duplicates": int(summary["duplicate"] or 0),
                "duplicate_updates": int(duplicate_row["rows"] or 0),
                "template_noise": int(summary["template_noise"] or 0),
                "parse_failed": int(summary["parse_failed"] or 0),
                "noise_pct": noise_pct,
                "duplicate_pct": duplicate_pct,
            },
        )
        run["quality_status"] = quality
        run["output_watermarks"] = {"corpus.document_quality": str(summary["data_through"])}
    print({**dict(summary), "processed": processed, "quality_status": quality})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

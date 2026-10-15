# -*- coding: utf-8 -*-
"""Rule-based target-price extraction from research report text."""
from __future__ import annotations

import argparse
import re
import calendar
import datetime as dt

from common import connect, jsonb


PATTERNS = [
    re.compile(r"(?:目标价|目标价格|合理价值|合理估值)[^0-9]{0,12}([0-9]+(?:\.[0-9]+)?)\s*(?:港元|美元|人民币元|元|CNY|HKD|USD)"),
    re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*元[^。；;\n]{0,16}(?:目标价|目标价格|合理价值)"),
]


def extract_price(text: str) -> float | None:
    if not text:
        return None
    for pattern in PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        try:
            value = float(m.group(1))
        except Exception:
            continue
        if 0 < value < 10000:
            return value
    return None


def target_terms(text,price,report_date):
    # Extract only an explicit nearby currency and a forward-looking horizon.
    positions=[m for m in re.finditer(r"(?:目标价|目标价格|合理价值|合理估值)[^。；;\n]{0,80}",text)
               if re.search(rf"(?<![0-9]){price:g}(?:0*)?(?![0-9])",m.group())]
    snippet=''
    if positions:
        match=positions[0]
        prefix=re.split(r'[。；;\n]',text[max(0,match.start()-60):match.start()])[-1]
        snippet=prefix+match.group()
    currency=next((code for term,code in [('人民币','CNY'),('CNY','CNY'),('港元','HKD'),('HKD','HKD'),('美元','USD'),('USD','USD')] if term in snippet),None)
    horizon=re.search(r'(?:未来|未来的|在)\s*(\d{1,2})\s*(?:个)?月',snippet)
    months=int(horizon.group(1)) if horizon and 1<=int(horizon.group(1))<=36 else None
    valid_until=None
    if months and report_date:
        year=report_date.year+(report_date.month-1+months)//12;month=(report_date.month-1+months)%12+1
        valid_until=dt.date(year,month,min(report_date.day,calendar.monthrange(year,month)[1])).isoformat()
    return {'target_currency':currency,'target_horizon_months':months,'target_valid_until':valid_until,
            'target_price_basis':'原文显式每股目标价；复权口径未说明','target_terms_evidence':snippet,
            'terms_parser':'explicit_currency_horizon:v1'}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2026-01-01")
    parser.add_argument("--until")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--method", default="target_price_rules:v4")
    args = parser.parse_args()

    updated = scanned = 0
    with connect(autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  r.document_id::text,
                  r.stock_code,
                  r.rating,
                  r.rating_change,
                  r.report_date,
                  concat_ws(E'\n', d.title, d.content_text, d.content_md, d.raw_text) AS body
                FROM research.reports_resolved r
                JOIN corpus.documents d ON d.id = r.document_id
                WHERE r.report_type = 'stock'
                  AND r.stock_code IS NOT NULL
                  AND r.report_date >= %s::date
                  AND (%s::date IS NULL OR r.report_date <= %s::date)
                  AND length(concat_ws(E'\n', d.title, d.content_text, d.content_md, d.raw_text)) > 50
                  AND (
                    %s OR NOT EXISTS (
                      SELECT 1 FROM kg.document_extraction_status s
                      WHERE s.document_id=r.document_id AND s.extraction_method=%s
                        AND s.status='ok' AND s.source_updated_at>=d.updated_at
                    )
                  )
                ORDER BY r.report_date DESC, r.document_id
                LIMIT %s
                """,
                (args.since, args.until, args.until, args.force, args.method, args.limit),
            )
            rows = cur.fetchall()
            for row in rows:
                scanned += 1
                price = extract_price(row["body"] or "")
                if price is not None:
                    terms=target_terms(row['body'] or '',price,row['report_date'])
                    cur.execute("""UPDATE research.report_ratings SET metadata=coalesce(metadata,'{}')||%s
                        WHERE document_id=%s AND target_price=%s""",(jsonb(terms),row['document_id'],price))
                    cur.execute(
                        """
                        INSERT INTO research.report_ratings (
                          document_id, stock_code, rating, rating_change,
                          target_price, extracted_by, confidence, metadata
                        )
                        SELECT %s,%s,%s,%s,%s,%s,0.65,%s
                        WHERE NOT EXISTS (
                          SELECT 1 FROM research.report_ratings existing
                          WHERE existing.document_id=%s AND existing.target_price IS NOT NULL
                        )
                        """,
                        (
                            row["document_id"], row["stock_code"], row["rating"], row["rating_change"],
                            price, args.method,
                            jsonb({"report_date": row["report_date"].isoformat() if row["report_date"] else None,**terms}),
                            row["document_id"],
                        ),
                    )
                    updated += max(cur.rowcount, 0)
                cur.execute(
                    """INSERT INTO kg.document_extraction_status
                         (document_id,extraction_method,status,event_count,keyword_count,processed_at,source_updated_at,details)
                       SELECT %s,%s,'ok',0,0,now(),updated_at,%s FROM corpus.documents WHERE id=%s
                       ON CONFLICT(document_id,extraction_method) DO UPDATE SET status='ok',processed_at=now(),
                         source_updated_at=EXCLUDED.source_updated_at,details=EXCLUDED.details""",
                    (row["document_id"],args.method,jsonb({"target_price_found":price is not None}),row["document_id"]),
                )
            conn.commit()
    print(f"scanned={scanned} target_prices={updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

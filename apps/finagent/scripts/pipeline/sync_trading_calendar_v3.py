#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import html
import re
from collections import defaultdict
from typing import Iterable
from zoneinfo import ZoneInfo

import requests
from psycopg.types.json import Jsonb

from common import connect
from v3_ops import record_quality, set_dataset_watermark, stage_run


TZ = ZoneInfo("Asia/Shanghai")
SSE_CLOSED_URL = "https://www.sse.com.cn/disclosure/dealinstruc/closed/"
EXCHANGES = ("SSE", "SZSE", "BSE")


def daterange(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    current = start
    while current <= end:
        yield current
        current += dt.timedelta(days=1)


def html_text(raw: str) -> str:
    value = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", raw)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def annual_section(text: str, year: int) -> str:
    marker = f"{year}年休市安排"
    start = text.find(marker)
    if start < 0:
        return ""
    match = re.search(r"20\d{2}年休市安排", text[start + len(marker):])
    end = start + len(marker) + match.start() if match else min(len(text), start + 8000)
    return text[start:end]


def closed_dates(section: str, year: int) -> set[dt.date]:
    closed: set[dt.date] = set()
    if not section:
        return closed
    # The heading itself contains “休市”; do not let it hide the first holiday.
    section = re.sub(r"^\s*\d{4}年休市安排\s*", "", section)
    for sentence in re.split(r"[。；;]", section):
        if "休市" not in sentence:
            continue
        before_closed = sentence.split("休市", 1)[0]
        ranges = list(re.finditer(
            r"(\d{1,2})月(\d{1,2})日[^，。；]{0,35}?至(?:(\d{1,2})月)?(\d{1,2})日",
            before_closed,
        ))
        consumed: list[tuple[int, int]] = []
        for match in ranges:
            start_month = int(match.group(1))
            end_month = int(match.group(3) or start_month)
            start_date = dt.date(year, start_month, int(match.group(2)))
            end_date = dt.date(year, end_month, int(match.group(4)))
            if end_date < start_date or (end_date - start_date).days > 30:
                continue
            closed.update(daterange(start_date, end_date))
            consumed.append(match.span())
        for match in re.finditer(r"(\d{1,2})月(\d{1,2})日", before_closed):
            if any(left <= match.start() and match.end() <= right for left, right in consumed):
                continue
            closed.add(dt.date(year, int(match.group(1)), int(match.group(2))))
    return closed


def fetch_official_closures(years: list[int], timeout: int = 25) -> tuple[dict[int, set[dt.date]], str | None]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; finance-agent-v3/1.0; local research archive)",
        "Referer": "https://www.sse.com.cn/",
    }
    try:
        response = requests.get(SSE_CLOSED_URL, headers=headers, timeout=timeout)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        text = html_text(response.text)
    except Exception as exc:  # noqa: BLE001
        return {}, f"{type(exc).__name__}: {exc}"
    result: dict[int, set[dt.date]] = {}
    for year in years:
        section = annual_section(text, year)
        dates = closed_dates(section, year)
        if section and dates:
            result[year] = dates
    return result, None


def observed_market_dates(start: dt.date, end: dt.date) -> set[dt.date]:
    with connect(admin=False) as conn:
        rows = conn.execute(
            """
            SELECT (b.ts AT TIME ZONE 'Asia/Shanghai')::date AS dt
            FROM market.bars_daily b
            JOIN market.assets a ON a.asset_id=b.asset_id
            WHERE a.asset_type IN ('stock','index')
              AND b.ts >= (%s::date::timestamp AT TIME ZONE 'Asia/Shanghai')
              AND b.ts < ((%s::date + 1)::timestamp AT TIME ZONE 'Asia/Shanghai')
            GROUP BY 1
            HAVING count(DISTINCT b.asset_id) >= 100
            """,
            (start, end),
        ).fetchall()
    return {row["dt"] for row in rows}


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize the official A-share trading calendar.")
    parser.add_argument("--since", default="2020-01-01")
    parser.add_argument("--until", default=(dt.date.today() + dt.timedelta(days=400)).isoformat())
    parser.add_argument("--strict", action="store_true", help="Fail when an official year section cannot be parsed.")
    args = parser.parse_args()
    since = dt.date.fromisoformat(args.since)
    until = dt.date.fromisoformat(args.until)
    if since > until:
        parser.error("--since must not be after --until")

    years = list(range(since.year, until.year + 1))
    closures_by_year, fetch_error = fetch_official_closures(years)
    observed = observed_market_dates(since, min(until, dt.date.today()))
    missing_official_years = sorted(set(years) - set(closures_by_year))
    if args.strict and missing_official_years:
        raise RuntimeError(
            f"official SSE closure sections unavailable for years={missing_official_years}: {fetch_error or 'parse failure'}"
        )

    rows = []
    unknown_weekdays: list[dt.date] = []
    inferred_historical_closures: list[dt.date] = []
    official_conflicts: list[dt.date] = []
    year_counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for day in daterange(since, until):
        is_weekend = day.weekday() >= 5
        official_known = day.year in closures_by_year
        officially_closed = day in closures_by_year.get(day.year, set()) or is_weekend
        observed_open = day in observed
        if official_known:
            status = "closed" if officially_closed else "open"
            verified = True
            source_name = "SSE annual market closure schedule"
        elif observed_open:
            status = "open"
            verified = False
            source_name = "observed market bars fallback"
        elif is_weekend:
            status = "closed"
            verified = False
            source_name = "weekday fallback"
        elif day < dt.date.today().replace(month=1, day=1):
            # The SSE page does not retain every old annual section.  A past
            # weekday without broad-market observations is an inferred closure,
            # not a current data outage.  Recent/current-year gaps remain hard
            # failures so missing dates such as 2026-08-05 cannot be hidden.
            status = "closed"
            verified = False
            source_name = "historical observed-calendar inference"
            inferred_historical_closures.append(day)
        else:
            status = "unknown"
            verified = False
            source_name = "unverified calendar gap"
            if day <= dt.date.today():
                unknown_weekdays.append(day)
        if observed_open and status == "closed":
            status = "unknown"
            verified = False
            official_conflicts.append(day)
        year_counts[day.year][status] += 1
        for exchange in EXCHANGES:
            open_at = dt.datetime.combine(day, dt.time(9, 30), TZ) if status == "open" else None
            close_at = dt.datetime.combine(day, dt.time(15, 0), TZ) if status == "open" else None
            rows.append((
                exchange, day, status, open_at, close_at, source_name,
                SSE_CLOSED_URL if official_known else None, verified,
                Jsonb({"official_year_available": official_known, "observed_open": observed_open}),
            ))

    expected_date = min(until, dt.date.today())
    with stage_run(
        "finance_v3_morning",
        "sync_trading_calendar",
        expected_date=expected_date,
        metadata={"since": since.isoformat(), "until": until.isoformat()},
    ) as run:
        with connect(admin=False) as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO market.trading_calendar (
                      exchange,trade_date,session_status,open_at,close_at,source_name,
                      source_url,is_verified,metadata,observed_at,available_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),now())
                    ON CONFLICT (exchange,trade_date) DO UPDATE SET
                      session_status=EXCLUDED.session_status,open_at=EXCLUDED.open_at,
                      close_at=EXCLUDED.close_at,source_name=EXCLUDED.source_name,
                      source_url=EXCLUDED.source_url,is_verified=EXCLUDED.is_verified,
                      metadata=EXCLUDED.metadata,observed_at=now(),available_at=now()
                    """,
                    rows,
                )
        quality = "pass" if not unknown_weekdays and not official_conflicts else "fail"
        record_quality(
            run["run_id"], "market.trading_calendar", "unknown_past_weekdays",
            status="pass" if not unknown_weekdays else "fail", severity="error",
            observed_value=float(len(unknown_weekdays)), threshold_value=0,
            affected_rows=len(unknown_weekdays),
            sample=[{"trade_date": value.isoformat()} for value in unknown_weekdays[:20]],
            details={"missing_official_years": missing_official_years, "fetch_error": fetch_error},
        )
        record_quality(
            run["run_id"], "market.trading_calendar", "official_observed_conflicts",
            status="pass" if not official_conflicts else "fail", severity="error",
            observed_value=float(len(official_conflicts)), threshold_value=0,
            affected_rows=len(official_conflicts),
            sample=[{"trade_date": value.isoformat()} for value in official_conflicts[:20]],
        )
        data_through = dt.datetime.combine(until, dt.time(23, 59), TZ)
        set_dataset_watermark(
            "market.trading_calendar", schema_version="v3", run_id=run["run_id"],
            expected_date=expected_date, data_through=data_through,
            row_count=len(rows), eligible_count=len(rows), coverage_pct=100.0,
            quality_status=quality, source_version="sse_closure_schedule:v1",
            details={
                "years": years, "missing_official_years": missing_official_years,
                "year_counts": year_counts, "fetch_error": fetch_error,
                "inferred_historical_closures": len(inferred_historical_closures),
            },
        )
        run["quality_status"] = quality
        run["output_watermarks"] = {"market.trading_calendar": until.isoformat()}
    print({
        "rows": len(rows), "range": f"{since}..{until}",
        "missing_official_years": missing_official_years,
        "unknown_past_weekdays": len(unknown_weekdays),
        "official_conflicts": len(official_conflicts),
        "inferred_historical_closures": len(inferred_historical_closures),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Restart-safe partitioned CNINFO equity-distribution backfill."""
from __future__ import annotations

import sys

import argparse
import datetime as dt
import subprocess
import time
from pathlib import Path

from common import ROOT, connect


JOB_NAME = "cninfo_corporate_actions_v2_title_coverage"
SEARCH_KEYS = ('权益分派', '利润分配', '分红派息')


def partitions(start: dt.date, end: dt.date, days: int):
    current = start
    while current <= end:
        partition_end = min(end, current + dt.timedelta(days=days - 1))
        yield current, partition_end
        current = partition_end + dt.timedelta(days=1)


def state(start: dt.date, end: dt.date):
    with connect(admin=False) as conn:
        return conn.execute(
            """
            SELECT status,attempt_count FROM ops.backfill_partitions_v3
            WHERE job_name=%s AND partition_start=%s AND partition_end=%s
            """,
            (JOB_NAME, start, end),
        ).fetchone()


def mark(
    start: dt.date,
    end: dt.date,
    status: str,
    *,
    row_count: int | None = None,
    error: str | None = None,
) -> None:
    with connect(admin=False) as conn:
        conn.execute(
            """
            INSERT INTO ops.backfill_partitions_v3 (
              job_name,partition_start,partition_end,status,attempt_count,
              started_at,completed_at,row_count,error_message,updated_at
            ) VALUES (%s,%s,%s,%s,1,
              CASE WHEN %s='running' THEN now() END,
              CASE WHEN %s IN ('succeeded','failed','skipped') THEN now() END,
              %s,%s,now())
            ON CONFLICT (job_name,partition_start,partition_end) DO UPDATE SET
              status=EXCLUDED.status,
              attempt_count=CASE WHEN EXCLUDED.status='running'
                THEN ops.backfill_partitions_v3.attempt_count+1
                ELSE ops.backfill_partitions_v3.attempt_count END,
              started_at=CASE WHEN EXCLUDED.status='running' THEN now()
                ELSE ops.backfill_partitions_v3.started_at END,
              completed_at=CASE WHEN EXCLUDED.status IN ('succeeded','failed','skipped')
                THEN now() ELSE NULL END,
              row_count=EXCLUDED.row_count,error_message=EXCLUDED.error_message,
              updated_at=now()
            """,
            (JOB_NAME, start, end, status, status, status, row_count, error),
        )


def run(command: list[str], timeout: int) -> str:
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}\n{output[-4000:]}")
    if output:
        print(output, flush=True)
    return output


def action_count(start: dt.date, end: dt.date) -> int:
    with connect(admin=False) as conn:
        row = conn.execute(
            """
            SELECT count(*) AS rows FROM market.corporate_actions
            WHERE announced_at::date BETWEEN %s AND %s AND source_name='cninfo'
            """,
            (start, end),
        ).fetchone()
    return int(row["rows"] or 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill official CNINFO corporate actions by partition.")
    parser.add_argument("--since", default="2020-01-01")
    parser.add_argument("--until", default=dt.date.today().isoformat())
    parser.add_argument("--chunk-days", type=int, default=92)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    args = parser.parse_args()
    since = dt.date.fromisoformat(args.since)
    until = dt.date.fromisoformat(args.until)
    if since > until:
        parser.error("--since cannot be after --until")

    python = str(Path(sys.executable))
    failures = 0
    completed = 0
    for start, end in partitions(since, until, max(1, args.chunk_days)):
        checkpoint = state(start, end)
        if checkpoint and checkpoint["status"] == "succeeded" and not args.force:
            print(f"skip succeeded {start}..{end}", flush=True)
            continue
        error = None
        for attempt in range(1, max(1, args.max_attempts) + 1):
            mark(start, end, "running")
            print(f"start {start}..{end} attempt={attempt}", flush=True)
            try:
                # Broad source queries plus the implementation-announcement
                # filter also catch “实施的公告” and “分红派息、转增股本”.
                # A separate checkpoint prevents old narrow-query successes
                # from silently suppressing this expanded coverage.
                for key in SEARCH_KEYS:
                    run([
                        python, "scripts/pipeline/daily_cninfo_filings.py",
                        "--start", start.isoformat(), "--end", end.isoformat(),
                        "--search-key", key, "--corporate-actions-only",
                        "--download-corporate-action-pdfs", "--partition-key", "history",
                    ], max(60, args.timeout))
                run([
                    python, "scripts/analytics/extract_corporate_actions_cninfo.py",
                    "--since", start.isoformat(), "--until", end.isoformat(),
                    "--limit", "20000", "--partition-key", "history",
                ], max(60, args.timeout))
                rows = action_count(start, end)
                mark(start, end, "succeeded", row_count=rows)
                print(f"complete {start}..{end} actions={rows}", flush=True)
                completed += 1
                error = None
                break
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"[-8000:]
                print(f"failed {start}..{end} attempt={attempt}: {error[-1200:]}", flush=True)
                if attempt < max(1, args.max_attempts):
                    time.sleep(min(30 * attempt, 120))
        if error:
            mark(start, end, "failed", error=error)
            failures += 1
            if not args.continue_on_error:
                break
        time.sleep(1)

    print({"job_name": JOB_NAME, "completed_partitions": completed, "failed_partitions": failures})
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

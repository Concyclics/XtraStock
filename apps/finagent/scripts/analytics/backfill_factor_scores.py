#!/usr/bin/env python3
"""Restart-safe monthly materialization of factor cross-sectional scores."""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import subprocess
import sys
import time

from common import connect, repo_root


def months(since: dt.date, until: dt.date):
    current = since.replace(day=1)
    while current <= until:
        end = dt.date(current.year, current.month, calendar.monthrange(current.year, current.month)[1])
        yield max(since, current), min(until, end)
        current = end + dt.timedelta(days=1)


def checkpoint(job: str, start: dt.date, end: dt.date):
    with connect() as conn:
        return conn.execute(
            """SELECT status FROM factor.backfill_partitions
               WHERE job_name=%s AND partition_start=%s AND partition_end=%s""",
            (job, start, end),
        ).fetchone()


def mark(job: str, start: dt.date, end: dt.date, status: str, output: str = "", error: str = "") -> None:
    with connect(autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO factor.backfill_partitions (
              job_name,partition_start,partition_end,status,attempt_count,
              started_at,finished_at,output_text,error_text,updated_at
            ) VALUES (%s,%s,%s,%s,1,
              CASE WHEN %s='running' THEN now() END,
              CASE WHEN %s IN ('complete','failed') THEN now() END,%s,%s,now())
            ON CONFLICT (job_name,partition_start,partition_end) DO UPDATE SET
              status=EXCLUDED.status,
              attempt_count=CASE WHEN EXCLUDED.status='running'
                                 THEN factor.backfill_partitions.attempt_count+1
                                 ELSE factor.backfill_partitions.attempt_count END,
              started_at=CASE WHEN EXCLUDED.status='running' THEN now()
                              ELSE factor.backfill_partitions.started_at END,
              finished_at=CASE WHEN EXCLUDED.status IN ('complete','failed') THEN now() ELSE NULL END,
              output_text=EXCLUDED.output_text,error_text=EXCLUDED.error_text,updated_at=now()
            """,
            (job, start, end, status, status, status, output[-8000:], error[-8000:]),
        )


def wait_for_temporal(since: dt.date, required_until: dt.date, poll_seconds: int) -> None:
    expected = {(start, end) for start, end in months(since, required_until)}
    while True:
        with connect() as conn:
            rows = conn.execute(
                """SELECT partition_start,partition_end,status,error_text
                   FROM kg.temporal_backfill_partitions
                   WHERE stage='temporal_graph_month' AND source_name='orchestrator'
                     AND partition_end>=%s AND partition_start<=%s""",
                (since, required_until),
            ).fetchall()
        failed = [row for row in rows if row["status"] == "failed"]
        if failed:
            latest = max(failed, key=lambda row: row["partition_start"])
            raise RuntimeError(
                f"temporal prerequisite failed at {latest['partition_start']}..{latest['partition_end']}: "
                f"{str(latest.get('error_text') or '')[-1000:]}"
            )
        complete = {(row["partition_start"], row["partition_end"]) for row in rows if row["status"] == "complete"}
        if expected <= complete:
            print(f"temporal prerequisite complete through {required_until}", flush=True)
            return
        missing = sorted(expected - complete)
        print(
            f"waiting for temporal prerequisite: complete={len(complete)}/{len(expected)} "
            f"next={missing[0][0]}..{missing[0][1]}",
            flush=True,
        )
        time.sleep(max(30, poll_seconds))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--status", default="shadow,production")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--wait-for-temporal-until")
    parser.add_argument("--poll-seconds", type=int, default=300)
    args = parser.parse_args()
    since, until = dt.date.fromisoformat(args.since), dt.date.fromisoformat(args.until)
    if since > until:
        parser.error("--since must not be after --until")
    statuses = sorted({value.strip() for value in args.status.split(",") if value.strip()})
    if not statuses or set(statuses) - {"candidate", "shadow", "production"}:
        parser.error("--status must contain candidate, shadow, or production")
    job = "factor_scores_v1:" + ",".join(statuses)
    status_arg = ",".join(statuses)

    if args.wait_for_temporal_until:
        wait_for_temporal(since, dt.date.fromisoformat(args.wait_for_temporal_until), args.poll_seconds)

    for start, end in months(since, until):
        state = checkpoint(job, start, end)
        if state and state["status"] == "complete" and not args.force:
            print(f"skip complete {start}..{end}", flush=True)
            continue
        mark(job, start, end, "running")
        command = [
            sys.executable, "scripts/analytics/build_factor_scores.py",
            "--since", start.isoformat(), "--until", end.isoformat(),
            "--status", status_arg,
        ]
        result = subprocess.run(command, cwd=repo_root(), text=True, capture_output=True)
        output = (result.stdout or "") + (result.stderr or "")
        print(output, end="", flush=True)
        if result.returncode:
            mark(job, start, end, "failed", error=output)
            raise RuntimeError(f"score backfill failed for {start}..{end}: {output[-2000:]}")
        mark(job, start, end, "complete", output=output)
        print(f"complete {start}..{end}", flush=True)

    with connect(autocommit=True, admin=True) as conn:
        conn.execute("ANALYZE factor.daily_scores")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Restart-safe historical backfill for causal technical and factor features."""
from __future__ import annotations

import sys

import argparse
import datetime as dt
import subprocess
from pathlib import Path

from common import connect, repo_root


def chunks(start: dt.date, end: dt.date, days: int):
    current = start
    while current <= end:
        chunk_end = min(end, current + dt.timedelta(days=days - 1))
        yield current, chunk_end
        current = chunk_end + dt.timedelta(days=1)


def checkpoint(job: str, start: dt.date, end: dt.date):
    with connect(autocommit=True) as conn:
        return conn.execute(
            """SELECT status, attempt_count FROM factor.backfill_partitions
               WHERE job_name=%s AND partition_start=%s AND partition_end=%s""",
            (job, start, end),
        ).fetchone()


def mark(job: str, start: dt.date, end: dt.date, status: str, output: str = "", error: str = "") -> None:
    with connect(autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO factor.backfill_partitions (
              job_name, partition_start, partition_end, status, attempt_count,
              started_at, finished_at, output_text, error_text, updated_at
            ) VALUES (%s,%s,%s,%s,1,
              CASE WHEN %s='running' THEN now() END,
              CASE WHEN %s IN ('complete','failed') THEN now() END,
              %s,%s,now())
            ON CONFLICT (job_name, partition_start, partition_end) DO UPDATE SET
              status=EXCLUDED.status,
              attempt_count=CASE WHEN EXCLUDED.status='running'
                                 THEN factor.backfill_partitions.attempt_count+1
                                 ELSE factor.backfill_partitions.attempt_count END,
              started_at=CASE WHEN EXCLUDED.status='running' THEN now()
                              ELSE factor.backfill_partitions.started_at END,
              finished_at=CASE WHEN EXCLUDED.status IN ('complete','failed') THEN now()
                               ELSE NULL END,
              output_text=EXCLUDED.output_text,
              error_text=EXCLUDED.error_text,
              updated_at=now()
            """,
            (job, start, end, status, status, status, output[-8000:], error[-8000:]),
        )


def run_partition(job: str, start: dt.date, end: dt.date, command: list[str], force: bool) -> None:
    state = checkpoint(job, start, end)
    if state and state["status"] == "complete" and not force:
        print(f"skip complete {job} {start}..{end}", flush=True)
        return
    mark(job, start, end, "running")
    print(f"start {job} {start}..{end}", flush=True)
    result = subprocess.run(command, cwd=repo_root(), text=True, capture_output=True)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode:
        mark(job, start, end, "failed", error=output)
        raise RuntimeError(f"{job} {start}..{end} failed: {output[-2000:]}")
    mark(job, start, end, "complete", output=output)
    print(output.strip(), flush=True)


def analyze_tables(*tables: str) -> None:
    with connect(autocommit=True, admin=True) as conn:
        for table in tables:
            conn.execute(f"ANALYZE {table}")
            print(f"analyzed {table}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--stages", default="technical,features")
    parser.add_argument("--technical-chunk-days", type=int, default=366)
    parser.add_argument("--feature-chunk-days", type=int, default=31)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    since = dt.date.fromisoformat(args.since)
    until = dt.date.fromisoformat(args.until)
    stages = {x.strip() for x in args.stages.split(",") if x.strip()}
    invalid = stages - {"technical", "features"}
    if invalid:
        parser.error(f"unsupported stages: {sorted(invalid)}")
    python = str(Path(sys.executable))

    if "technical" in stages:
        for start, end in chunks(since, until, max(1, args.technical_chunk_days)):
            run_partition(
                "technical_causal_v2", start, end,
                [python, "scripts/analytics/build_technical_signals.py",
                 "--since", start.isoformat(), "--until", end.isoformat()],
                args.force,
            )
        analyze_tables("analytics.asset_technical_daily")
    if "features" in stages:
        for start, end in chunks(since, until, max(1, args.feature_chunk_days)):
            run_partition(
                "factor_features_v1", start, end,
                [python, "scripts/analytics/build_factor_features.py",
                 "--since", start.isoformat(), "--until", end.isoformat(),
                 "--chunk-days", str(max(1, args.feature_chunk_days))],
                args.force,
            )
        analyze_tables("factor.feature_daily", "factor.forward_returns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

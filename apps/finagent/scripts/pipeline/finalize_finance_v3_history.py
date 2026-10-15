#!/usr/bin/env python3
"""Finish the V3 historical rebuild after official action PDFs are available."""
from __future__ import annotations

import sys

import argparse
import datetime as dt
import subprocess
import time
from pathlib import Path

from common import ROOT, connect


ACTION_JOB = "cninfo_corporate_actions_v1"


def partitions(start: dt.date, end: dt.date, days: int):
    current = start
    while current <= end:
        partition_end = min(end, current + dt.timedelta(days=days - 1))
        yield current, partition_end
        current = partition_end + dt.timedelta(days=1)


def run(command: list[str], timeout: int | None = None) -> None:
    print("$", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True, timeout=timeout)


def wait_for_actions(
    since: dt.date, until: dt.date, chunk_days: int, poll_seconds: int
) -> None:
    expected = set(partitions(since, until, chunk_days))
    last_progress: tuple[int, int, int] | None = None
    while True:
        with connect(admin=False) as conn:
            rows = conn.execute(
                """
                SELECT partition_start,partition_end,status,error_message
                FROM ops.backfill_partitions_v3
                WHERE job_name=%s AND partition_end >= %s AND partition_start <= %s
                """,
                (ACTION_JOB, since, until),
            ).fetchall()
        failed = [row for row in rows if row["status"] == "failed"]
        if failed:
            row = min(failed, key=lambda item: item["partition_start"])
            raise RuntimeError(
                f"official action backfill failed at {row['partition_start']}.."
                f"{row['partition_end']}: {str(row['error_message'] or '')[-1000:]}"
            )
        succeeded = {
            (row["partition_start"], row["partition_end"])
            for row in rows if row["status"] == "succeeded"
        }
        running = sum(row["status"] == "running" for row in rows)
        progress = (len(succeeded), len(expected), running)
        if progress != last_progress:
            print(
                f"official actions: succeeded={len(succeeded)}/{len(expected)} "
                f"running={running}",
                flush=True,
            )
            last_progress = progress
        if expected <= succeeded:
            return
        time.sleep(max(30, poll_seconds))


def assert_action_quality(until: dt.date) -> None:
    with connect(admin=False) as conn:
        row = conn.execute(
            """
            SELECT expected_date,coverage_pct,quality_status,details
            FROM ops.dataset_watermarks
            WHERE dataset_key='market.corporate_actions' AND partition_key='history'
            """
        ).fetchone()
    if not row:
        raise RuntimeError("missing historical corporate-action watermark")
    coverage = float(row["coverage_pct"] or 0)
    if row["expected_date"] < until or coverage < 99.0 or row["quality_status"] != "pass":
        raise RuntimeError(
            "historical corporate-action gate failed: "
            f"expected_date={row['expected_date']} coverage={coverage:.3f}% "
            f"quality={row['quality_status']} details={row['details']}"
        )
    print(f"official action parse gate passed: coverage={coverage:.3f}%", flush=True)


def production_factor_count() -> int:
    with connect(admin=False) as conn:
        row = conn.execute(
            "SELECT count(*) AS rows FROM factor.definitions WHERE lifecycle_status='production'"
        ).fetchone()
    return int(row["rows"] or 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Strictly finalize Finance Agent V3 history.")
    parser.add_argument("--since", default="2020-01-01")
    parser.add_argument("--until", default=dt.date.today().isoformat())
    parser.add_argument("--chunk-days", type=int, default=92)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--skip-wait", action="store_true")
    args = parser.parse_args()
    since = dt.date.fromisoformat(args.since)
    until = dt.date.fromisoformat(args.until)
    if since > until:
        parser.error("--since cannot be after --until")
    if production_factor_count() != 0:
        raise RuntimeError("production factors already exist; refusing an unattended historical rebuild")

    python = str(Path(sys.executable))
    if not args.skip_wait:
        wait_for_actions(since, until, max(1, args.chunk_days), args.poll_seconds)
    run([
        python, "scripts/analytics/extract_corporate_actions_cninfo.py",
        "--since", since.isoformat(), "--until", until.isoformat(),
        "--limit", "100000", "--partition-key", "history",
    ], timeout=6 * 3600)
    assert_action_quality(until)
    run([
        python, "scripts/search/rebuild_meili_v2.py",
        "--stable-index", "finance_documents_v2", "--fetch-size", "5000",
        "--batch-size", "1000", "--min-coverage-pct", "99",
        "--skip-if-current-coverage",
    ], timeout=12 * 3600)
    run([
        python, "scripts/analytics/apply_schema.py", "--admin", "--sql",
        "docker/postgres/migrate_finance_agent_v3_runtime.sql",
    ], timeout=3600)
    run([
        python, "scripts/analytics/build_adjustment_factors_v3.py",
        "--since", since.isoformat(), "--until", until.isoformat(),
    ], timeout=8 * 3600)
    run([
        python, "scripts/analytics/backfill_factor_pipeline.py",
        "--since", since.isoformat(), "--until", until.isoformat(),
        "--stages", "technical,features", "--force",
    ], timeout=24 * 3600)
    run([
        python, "scripts/analytics/backfill_factor_scores.py",
        "--since", since.isoformat(), "--until", until.isoformat(),
        "--status", "shadow,production", "--force",
    ], timeout=24 * 3600)
    run([
        python, "scripts/analytics/refresh_v3_preaggregates.py",
        "--as-of", until.isoformat(),
    ], timeout=3600)
    run([
        python, "scripts/pipeline/evaluate_data_gate_v3.py",
        "--date", until.isoformat(),
    ], timeout=3600)
    if production_factor_count() != 0:
        raise RuntimeError("unattended rebuild changed production factor count")
    print("Finance Agent V3 historical finalization complete; production factors=0", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

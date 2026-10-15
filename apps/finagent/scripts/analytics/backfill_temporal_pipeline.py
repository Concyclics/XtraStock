#!/usr/bin/env python3
"""Restart-safe monthly backfill for temporal KG and graph factors."""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import re
import subprocess
import sys

from common import connect, repo_root


def months(since: dt.date, until: dt.date):
    current = since.replace(day=1)
    while current <= until:
        end = dt.date(current.year, current.month, calendar.monthrange(current.year, current.month)[1])
        yield max(since, current), min(until, end)
        current = (end + dt.timedelta(days=1)).replace(day=1)


def run(args: list[str]) -> str:
    proc = subprocess.run(args, cwd=repo_root(), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = proc.stdout or ""
    print(output, end="", flush=True)
    if proc.returncode:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(args)}\n{output[-2000:]}")
    return output


def rules_month(start: dt.date, end: dt.date, batch_size: int) -> None:
    while True:
        output = run([
            sys.executable, "scripts/analytics/extract_document_events_rules.py",
            "--since", start.isoformat(), "--until", end.isoformat(),
            "--limit", str(batch_size),
        ])
        match = re.search(r"documents=(\d+)", output)
        if match and int(match.group(1)) == 0:
            return
        if not match:
            raise RuntimeError("rules extractor did not report document count")


def target_prices_month(start: dt.date, end: dt.date, batch_size: int) -> None:
    while True:
        output = run([
            sys.executable, "scripts/analytics/extract_research_target_price.py",
            "--since", start.isoformat(), "--until", end.isoformat(),
            "--limit", str(batch_size),
        ])
        match = re.search(r"scanned=(\d+)", output)
        if match and int(match.group(1)) == 0:
            return
        if not match:
            raise RuntimeError("target-price extractor did not report scan count")


def checkpoint(stage: str, start: dt.date, end: dt.date, status: str, error: str | None = None) -> None:
    with connect(autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO kg.temporal_backfill_partitions
              (stage,source_name,partition_start,partition_end,status,attempt_count,started_at,finished_at,error_text,updated_at)
            VALUES (%s,'orchestrator',%s,%s,%s,1,
                    CASE WHEN %s='running' THEN now() END,
                    CASE WHEN %s IN ('complete','failed') THEN now() END,%s,now())
            ON CONFLICT (stage,source_name,partition_start,partition_end) DO UPDATE SET
              status=EXCLUDED.status,
              attempt_count=CASE WHEN EXCLUDED.status='running' THEN kg.temporal_backfill_partitions.attempt_count+1
                                 ELSE kg.temporal_backfill_partitions.attempt_count END,
              started_at=CASE WHEN EXCLUDED.status='running' THEN now() ELSE kg.temporal_backfill_partitions.started_at END,
              finished_at=EXCLUDED.finished_at,error_text=EXCLUDED.error_text,updated_at=now()
            """,
            (stage, start, end, status, status, status, error[:2000] if error else None),
        )


def already_complete(stage: str, start: dt.date, end: dt.date) -> bool:
    with connect() as conn:
        row = conn.execute(
            """SELECT status FROM kg.temporal_backfill_partitions
               WHERE stage=%s AND source_name='orchestrator' AND partition_start=%s AND partition_end=%s""",
            (stage, start, end),
        ).fetchone()
    return bool(row and row["status"] == "complete")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2020-01-01")
    parser.add_argument("--until", default=dt.date.today().isoformat())
    parser.add_argument("--rules-batch-size", type=int, default=5000)
    parser.add_argument("--candidate-limit", type=int, default=20000)
    parser.add_argument("--skip-rules", action="store_true")
    parser.add_argument("--skip-candidates", action="store_true")
    parser.add_argument("--skip-features", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    since, until = dt.date.fromisoformat(args.since), dt.date.fromisoformat(args.until)

    for start, end in months(since, until):
        stage = "temporal_graph_month"
        if already_complete(stage, start, end) and not args.force:
            print(f"skip complete {start}..{end}", flush=True)
            continue
        checkpoint(stage, start, end, "running")
        try:
            if not args.skip_rules:
                rules_month(start, end, max(100, args.rules_batch_size))
                target_prices_month(start, end, max(100, args.rules_batch_size))
            run([sys.executable, "scripts/analytics/build_temporal_knowledge_graph.py",
                 "--since", start.isoformat(), "--until", end.isoformat()])
            if not args.skip_features:
                run([sys.executable, "scripts/analytics/build_temporal_graph_features_daily.py",
                     "--since", start.isoformat(), "--until", end.isoformat()])
                run([sys.executable, "scripts/analytics/build_factor_features.py",
                     "--since", start.isoformat(), "--until", end.isoformat(),
                     "--chunk-days", "31", "--skip-labels"])
            checkpoint(stage, start, end, "complete")
        except Exception as exc:
            checkpoint(stage, start, end, "failed", str(exc))
            raise
    if not args.skip_candidates:
        run([sys.executable, "scripts/analytics/queue_temporal_llm_candidates.py",
             "--since", since.isoformat(), "--until", until.isoformat(),
             "--limit", str(args.candidate_limit)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

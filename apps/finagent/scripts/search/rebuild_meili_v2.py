#!/usr/bin/env python3
from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/pipeline"))

from common import connect, load_env  # noqa: E402
from v3_ops import record_quality, set_dataset_watermark, stage_run  # noqa: E402


def headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def request(method: str, url: str, key: str, **kwargs: Any) -> requests.Response:
    response = requests.request(method, url, headers=headers(key), timeout=120, **kwargs)
    response.raise_for_status()
    return response


def wait_task(base_url: str, key: str, task_uid: int, timeout: int = 600) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = request("GET", f"{base_url}/tasks/{task_uid}", key).json()
        if payload.get("status") == "succeeded":
            return
        if payload.get("status") in {"failed", "canceled"}:
            raise RuntimeError(f"Meilisearch task failed: {payload}")
        time.sleep(1)
    raise TimeoutError(f"Meilisearch task {task_uid} timed out")


def task_uid(response: requests.Response) -> int:
    payload = response.json()
    return int(payload.get("taskUid", payload.get("uid")))


def ensure_empty_stable(base_url: str, key: str, index: str) -> None:
    response = requests.get(f"{base_url}/indexes/{index}", headers=headers(key), timeout=30)
    if response.status_code == 404:
        created = request("POST", f"{base_url}/indexes", key, json={"uid": index, "primaryKey": "id"})
        wait_task(base_url, key, task_uid(created))
    elif response.status_code >= 400:
        response.raise_for_status()


def current_index_count(base_url: str, key: str, index: str) -> int | None:
    response = requests.get(f"{base_url}/indexes/{index}/stats", headers=headers(key), timeout=30)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return int(response.json().get("numberOfDocuments") or 0)


def eligible_documents() -> int:
    with connect(admin=False) as conn:
        row = conn.execute(
            """
            SELECT count(*) AS rows
            FROM corpus.documents d
            WHERE NOT EXISTS (
              SELECT 1 FROM corpus.document_quarantine q
              WHERE q.document_id=d.id AND q.released_at IS NULL
            )
              AND NOT EXISTS (
                SELECT 1 FROM corpus.document_quality dq
                WHERE dq.document_id=d.id
                  AND dq.quality_status IN ('template_noise','parse_failed','quarantined')
              )
            """
        ).fetchone()
    return int(row["rows"] or 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Atomically rebuild the full Finance Agent V3 Meilisearch index.")
    parser.add_argument("--stable-index", default="finance_documents_v2")
    parser.add_argument("--fetch-size", type=int, default=500)
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--body-chars", type=int, default=6000)
    parser.add_argument("--min-coverage-pct", type=float, default=99.0)
    parser.add_argument(
        "--skip-if-current-coverage",
        action="store_true",
        help="Keep the stable index when its live coverage already meets the threshold.",
    )
    args = parser.parse_args()

    env = load_env()
    base_url = os.getenv("MEILI_URL", env.get("MEILI_URL", "http://127.0.0.1:17700")).rstrip("/")
    key = os.getenv("MEILI_MASTER_KEY", env.get("MEILI_MASTER_KEY", ""))
    if not key:
        raise SystemExit("missing MEILI_MASTER_KEY")
    suffix = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    build_index = f"{args.stable_index}_build_{suffix}"
    with stage_run("finance_v3_search", "rebuild_meili_v2") as run:
        eligible = eligible_documents()
        if args.skip_if_current_coverage:
            indexed = current_index_count(base_url, key, args.stable_index)
            coverage = 100.0 * indexed / eligible if indexed is not None and eligible else 0.0
            if indexed is not None and coverage >= args.min_coverage_pct:
                now = dt.datetime.now(dt.timezone.utc)
                record_quality(
                    run["run_id"], "corpus.meili_finance_documents_v2", "live_index_coverage",
                    status="pass", severity="error", observed_value=coverage,
                    threshold_value=args.min_coverage_pct,
                    affected_rows=max(eligible-indexed, 0),
                    details={"stable_index": args.stable_index, "rebuild_skipped": True},
                )
                set_dataset_watermark(
                    "corpus.meili_finance_documents_v2", schema_version="v2", run_id=run["run_id"],
                    expected_date=now.date(), data_through=now,row_count=indexed,
                    eligible_count=eligible,coverage_pct=coverage,quality_status="pass",
                    source_version=f"meilisearch:{args.stable_index}",
                    details={"atomic_swap": False, "rebuild_skipped": True},
                )
                run["quality_status"] = "pass"
                run["output_watermarks"] = {
                    "corpus.meili_finance_documents_v2": now.isoformat()
                }
                print(json.dumps({
                    "stable_index": args.stable_index, "indexed": indexed,
                    "eligible": eligible, "coverage_pct": round(coverage, 3),
                    "swapped": False, "rebuild_skipped": True,
                }, ensure_ascii=False))
                return 0
        command = [
            str(ROOT / sys.executable),
            str(ROOT / "scripts/search/sync_document_search.py"),
            "--skip-schema", "--skip-fts", "--index", build_index,
            "--fetch-size", str(max(10, args.fetch_size)),
            "--meili-batch-size", str(max(10, args.batch_size)),
            "--body-chars", str(max(500, args.body_chars)),
            "--meili-body-chars", str(max(500, args.body_chars)),
        ]
        print("$", " ".join(command))
        subprocess.run(command, cwd=ROOT, check=True)

        stats = request("GET", f"{base_url}/indexes/{build_index}/stats", key).json()
        indexed = int(stats.get("numberOfDocuments") or 0)
        eligible = eligible_documents()
        coverage = 100.0 * indexed / eligible if eligible else 0.0
        quality = "pass" if coverage >= args.min_coverage_pct else "fail"
        record_quality(
            run["run_id"], "corpus.meili_finance_documents_v2", "full_index_coverage",
            status=quality, severity="error", observed_value=coverage,
            threshold_value=args.min_coverage_pct, affected_rows=max(eligible-indexed, 0),
            details={"build_index": build_index, "stable_index": args.stable_index},
        )
        if quality == "fail":
            raise RuntimeError(
                f"V3 Meili build coverage below threshold: indexed={indexed} "
                f"eligible={eligible} coverage={coverage:.2f}%"
            )

        ensure_empty_stable(base_url, key, args.stable_index)
        swapped = request(
            "POST", f"{base_url}/swap-indexes", key,
            json=[{"indexes": [args.stable_index, build_index]}],
        )
        wait_task(base_url, key, task_uid(swapped))
        deleted = request("DELETE", f"{base_url}/indexes/{build_index}", key)
        wait_task(base_url, key, task_uid(deleted))
        now = dt.datetime.now(dt.timezone.utc)
        set_dataset_watermark(
            "corpus.meili_finance_documents_v2", schema_version="v2", run_id=run["run_id"],
            expected_date=now.date(), data_through=now,row_count=indexed,
            eligible_count=eligible,coverage_pct=coverage,quality_status="pass",
            source_version=f"meilisearch:{args.stable_index}",
            details={"atomic_swap": True, "body_chars": args.body_chars},
        )
        run["quality_status"] = "pass"
        run["output_watermarks"] = {"corpus.meili_finance_documents_v2": now.isoformat()}
    print(json.dumps({
        "stable_index": args.stable_index, "indexed": indexed,
        "eligible": eligible, "coverage_pct": round(coverage, 3), "swapped": True,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

from common import ROOT, connect, ensure_ops_schema, log_path, run_logged
sys.path.insert(0, str(ROOT))
from report_attachments import validate_outputs


def claim_many(max_retries: int, limit: int, queue_ids: list[int] | None = None) -> list[dict]:
    with connect(admin=False) as conn:
        rows = conn.execute(
            """
            WITH candidate AS (
              SELECT id
              FROM ops.ocr_queue
              WHERE status = 'pending'
                AND retry_count < %s
                AND (%s::bigint[] IS NULL OR id = ANY(%s::bigint[]))
              ORDER BY priority, discovered_at, id
              FOR UPDATE SKIP LOCKED
              LIMIT %s
            )
            UPDATE ops.ocr_queue q
            SET status = 'running',
                started_at = now(),
                updated_at = now(),
                last_error = NULL
            FROM candidate
            WHERE q.id = candidate.id
            RETURNING q.*
            """,
            (max_retries, queue_ids, queue_ids, limit),
        ).fetchall()
    return list(rows)


def mark_ok(queue_id: int) -> None:
    with connect(admin=False) as conn:
        conn.execute(
            """
            UPDATE ops.ocr_queue
            SET status = 'ok',
                finished_at = now(),
                updated_at = now(),
                last_error = NULL
            WHERE id = %s
            """,
            (queue_id,),
        )


def mark_failed(queue_id: int, error: str, max_retries: int) -> None:
    with connect(admin=False) as conn:
        conn.execute(
            """
            UPDATE ops.ocr_queue
            SET status = CASE WHEN retry_count + 1 >= %s THEN 'failed' ELSE 'pending' END,
                retry_count = retry_count + 1,
                last_error = %s,
                finished_at = now(),
                updated_at = now()
            WHERE id = %s
            """,
            (max_retries, error[:4000], queue_id),
        )


def reset_stale_running(stale_minutes: int, queue_ids: list[int] | None = None) -> int:
    with connect(admin=False) as conn:
        row = conn.execute(
            """
            UPDATE ops.ocr_queue
            SET status = 'pending',
                updated_at = now(),
                last_error = 'reset stale running task'
            WHERE status = 'running'
              AND started_at < now() - (%s || ' minutes')::interval
              AND (%s::bigint[] IS NULL OR id = ANY(%s::bigint[]))
            RETURNING id
            """,
            (stale_minutes, queue_ids, queue_ids),
        ).fetchall()
    return len(row)


def output_dir_for(report_type: str) -> Path:
    if report_type == 'announcement':
        return ROOT / 'filings/cninfo_ocr'
    return ROOT / ("PDF_md" if report_type == "stock" else "PDF_ind_md")


def sync_reports(log_file: Path) -> None:
    run_logged(
        [
            sys.executable,
            "scripts/pipeline/incremental_import.py",
            "--source", "eastmoney",
        ],
        log_file,
    )


def process_item(item: dict, args: argparse.Namespace) -> bool:
    queue_id = item["id"]
    pdf_path = Path(item["pdf_path"])
    log_file = log_path(f"ocr_{item['report_type']}")
    cmd = [
        sys.executable,
        "-u",
        "pdf_ocr_markdown.py",
        str(pdf_path),
        "-o",
        str(output_dir_for(item["report_type"])),
        "--workers",
        str(args.pdf_workers),
        "--sort-by",
        "mtime",
        "--min-age-seconds",
        # CNINFO attachments are queued only after an atomic completed download.
        # Applying the directory-watcher age gate to them consumes all retries
        # while a perfectly valid newly downloaded PDF is still <10 minutes old.
        str(0 if item.get('source_name') == 'cninfo' else args.min_age_seconds),
        "--max-tokens",
        str(max(1024,min(6144,int((item.get('metadata') or {}).get('ocr_max_tokens',4096))))),
        "--warning-log",
        str(output_dir_for(item["report_type"]) / "ocr_worker_warnings.jsonl"),
    ]
    rotation = (item.get('metadata') or {}).get('ocr_rotate_clockwise', 0)
    if rotation:
        cmd.extend(["--rotate-clockwise", str(rotation)])
    metadata=item.get('metadata') or {}
    for page,angle in metadata.get('ocr_page_rotations',{}).items():
        cmd.extend(['--page-rotation',f'{int(page)}:{int(angle)}'])
    for page in metadata.get('ocr_confirmed_blank_pages',[]):
        cmd.extend(['--confirmed-blank-page',str(int(page))])
    if metadata.get('ocr_confirmed_blank_pages'):
        cmd.extend(['--blank-source-sha256',str(metadata.get('ocr_reviewed_source_sha256',''))])
    # Page cache reuse verifies both source and output hashes before skipping.
    # Every retry should resume validated pages, not repeat a 200-page report.
    cmd.append('--retry-failed-pages')
    if metadata.get('ocr_prompt'):
        cmd.extend(['--prompt',str(metadata['ocr_prompt'])])
    if args.no_figure_pass or metadata.get('ocr_no_figure_pass'):
        cmd.append("--no-figure-pass")
    try:
        run_logged(cmd, log_file)
        validate_outputs(pdf_path, Path(item["md_path"]), Path(item["quality_json_path"]))
        if item['source_name'] == 'cninfo':
            from sync_cninfo_document_bodies import persist
            document_id=(item.get('metadata') or {}).get('document_id')
            if not document_id:
                raise ValueError('CNINFO OCR item has no document identity')
            with connect() as conn:
                row=conn.execute('SELECT id,title,source_type,source_name,metadata FROM corpus.documents WHERE id=%s',(document_id,)).fetchone()
            if not row:raise ValueError('CNINFO document no longer exists')
            persist(row,{'text':Path(item['md_path']).read_text(),'path':str(pdf_path),'method':'ocr:validated_markdown'})
        mark_ok(queue_id)
        print(f"[ocr_worker] ok id={queue_id} pdf={pdf_path}")
        return True
    except (Exception, subprocess.CalledProcessError) as exc:  # noqa: BLE001
        detail = str(exc)
        if isinstance(exc, subprocess.CalledProcessError) and log_file.exists():
            # Keep the underlying parser/backend error rather than only exit 1.
            lines = log_file.read_text(errors="replace").splitlines()
            errors = [line for line in lines if "ERROR" in line or "Error:" in line]
            detail = ("; ".join(errors[-2:]) or detail) + f" [log: {log_file}]"
        mark_failed(queue_id, detail, args.max_retries)
        print(f"[ocr_worker] failed id={queue_id} pdf={pdf_path}: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Continuously process ops.ocr_queue.")
    parser.add_argument("--limit", type=int, default=0, help="Exit after processing this many PDFs. 0 means forever.")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--stale-minutes", type=int, default=360)
    parser.add_argument("--concurrency", type=int, default=4, help="Number of queue PDFs to OCR concurrently.")
    parser.add_argument("--pdf-workers", type=int, default=1, help="Passed to pdf_ocr_markdown.py for each claimed PDF.")
    parser.add_argument("--sync-every", type=int, default=100, help="Run report import after this many successful OCR jobs. 0 disables.")
    parser.add_argument("--no-figure-pass", action="store_true")
    parser.add_argument("--queue-id", type=int, action="append", help="Only process these explicitly selected jobs.")
    parser.add_argument("--min-age-seconds", type=int, default=600)
    args = parser.parse_args()

    ensure_ops_schema()
    processed = 0
    successes_since_sync = 0
    failures = 0
    idle_rounds=0;last_idle_log=0.0
    reset = reset_stale_running(args.stale_minutes, args.queue_id)
    if reset:
        print(f"[ocr_worker] reset stale running rows: {reset}")

    pending: dict[Future[bool], dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as executor:
        while True:
            while len(pending) < max(1, args.concurrency):
                if args.limit and processed + len(pending) >= args.limit:
                    break
                slots = max(1, args.concurrency) - len(pending)
                if args.limit:
                    slots = min(slots, args.limit - processed - len(pending))
                items = claim_many(args.max_retries, max(1, slots), args.queue_id)
                if not items:
                    break
                for item in items:
                    pending[executor.submit(process_item, item, args)] = item

            if not pending:
                if args.queue_id or (args.limit and processed >= args.limit):
                    break
                idle_rounds+=1
                delay=min(300,args.poll_seconds*2**min(idle_rounds-1,5))
                if time.monotonic()-last_idle_log>=1800:
                    print(f"[ocr_worker] idle; polling backs off to {delay}s")
                    last_idle_log=time.monotonic()
                time.sleep(delay)
                continue

            idle_rounds=0
            done, _ = wait(pending, timeout=args.poll_seconds, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                item = pending.pop(future)
                ok = future.result()
                processed += 1
                if ok and item['source_name'] != 'cninfo':
                    successes_since_sync += 1
                elif not ok:
                    failures += 1
                if args.sync_every and successes_since_sync >= args.sync_every:
                    sync_reports(log_path("ocr_sync"))
                    successes_since_sync = 0

            if args.limit and processed >= args.limit:
                break

    if args.sync_every and successes_since_sync:
        sync_reports(log_path("ocr_sync"))
    print(f"[ocr_worker] processed={processed} failed_attempts={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

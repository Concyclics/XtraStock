from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


ROOT = Path(__file__).resolve().parents[2]
TZ = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")
_SCHEMA_LOCK = threading.Lock()
_OPS_SCHEMA_ADVISORY_LOCK_ID = 70012001
_OPS_SCHEMA_READY = False


def today() -> dt.date:
    return dt.datetime.now(TZ).date()


def ymd(value: dt.date) -> str:
    return value.strftime("%Y%m%d")


def iso(value: dt.date) -> str:
    return value.isoformat()


def load_env(path: Path | None = None) -> dict[str, str]:
    from xtrastock.config import load_legacy_env
    return load_legacy_env(path)


def pg_kwargs(admin: bool = False) -> dict[str, Any]:
    from xtrastock.config import Settings
    return {**Settings().postgres_kwargs('admin' if admin else 'app'), 'row_factory': dict_row}


def connect(admin: bool = False):
    kwargs = pg_kwargs(admin=admin)
    kwargs["connect_timeout"] = 10
    last_error: Exception | None = None
    for attempt in range(20):
        try:
            return psycopg.connect(**kwargs)
        except psycopg.OperationalError as exc:
            last_error = exc
            if attempt < 19:
                time.sleep(min(0.5 * (attempt + 1), 3.0))
    assert last_error is not None
    raise last_error


def _finalize_crawler_run(run_id: int, status: str, new_rows: int | None, error: str | None = None) -> None:
    with connect(admin=False) as conn:
        conn.execute(
            """
            UPDATE ops.crawler_runs
            SET status = %s,
                finished_at = now(),
                new_rows = %s,
                error = %s
            WHERE id = %s
            """,
            (status, new_rows, error, run_id),
        )


def recover_stale_crawler_runs(
    task_names: list[str] | tuple[str, ...] | None = None,
    *,
    stale_minutes: int = 180,
) -> int:
    """Close abandoned running rows before a retry starts.

    A oneshot process can be killed by systemd or the host before the context
    manager gets a chance to finalize its row.  New runs must not inherit that
    stale state as a successful/active dependency.
    """
    if stale_minutes <= 0:
        raise ValueError("stale_minutes must be positive")
    clauses = [
        "status = 'running'",
        "started_at < now() - (%s * interval '1 minute')",
    ]
    params: list[Any] = [stale_minutes]
    if task_names:
        clauses.append("task_name = ANY(%s)")
        params.append(list(task_names))
    with connect(admin=False) as conn:
        row = conn.execute(
            f"""
            WITH recovered AS (
              UPDATE ops.crawler_runs
              SET status = 'failed',
                  finished_at = now(),
                  error = COALESCE(error, 'recovered stale running status before retry')
              WHERE {' AND '.join(clauses)}
              RETURNING id
            )
            SELECT count(*) AS rows FROM recovered
            """,
            params,
        ).fetchone()
    return int(row["rows"] if row else 0)


def ensure_ops_schema() -> None:
    global _OPS_SCHEMA_READY
    if _OPS_SCHEMA_READY:
        return
    with connect(admin=False) as conn:
        row = conn.execute(
            "SELECT to_regclass('ops.pipeline_watermarks') IS NOT NULL AS ready"
        ).fetchone()
    if row and row["ready"]:
        _OPS_SCHEMA_READY = True
        return

    sql = (ROOT / "docker/postgres/schema_ops_pipeline.sql").read_text(encoding="utf-8")
    with _SCHEMA_LOCK:
        if _OPS_SCHEMA_READY:
            return
        with connect(admin=True) as conn:
            conn.autocommit = True
            conn.execute("SET lock_timeout = '30s'")
            conn.execute("SELECT pg_advisory_lock(%s)", (_OPS_SCHEMA_ADVISORY_LOCK_ID,))
            try:
                conn.execute(sql)
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (_OPS_SCHEMA_ADVISORY_LOCK_ID,))
        _OPS_SCHEMA_READY = True


@contextmanager
def pipeline_lock(pipeline_name: str) -> Iterator[None]:
    """Prevent overlapping runs of one logical pipeline across systemd/manual starts."""
    conn = connect(admin=False)
    conn.autocommit = True
    acquired = False
    try:
        row = conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired",
            (f"finance-pipeline:{pipeline_name}",),
        ).fetchone()
        acquired = bool(row and row["acquired"])
        if not acquired:
            raise RuntimeError(f"pipeline already running: {pipeline_name}")
        yield
    finally:
        if acquired:
            conn.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                (f"finance-pipeline:{pipeline_name}",),
            )
        conn.close()


def get_pipeline_watermark(pipeline_name: str) -> dict[str, Any] | None:
    ensure_ops_schema()
    with connect(admin=False) as conn:
        row = conn.execute(
            "SELECT * FROM ops.pipeline_watermarks WHERE pipeline_name = %s",
            (pipeline_name,),
        ).fetchone()
    return dict(row) if row else None


def set_pipeline_watermark(
    pipeline_name: str,
    status: str,
    *,
    run_id: int | None = None,
    source_high_watermark: dt.datetime | None = None,
    dirty_from_date: dt.date | None = None,
    dirty_to_date: dt.date | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    if status not in {"pending", "running", "ok", "failed"}:
        raise ValueError(f"invalid pipeline status: {status}")
    with connect(admin=False) as conn:
        conn.execute(
            """
            INSERT INTO ops.pipeline_watermarks (
              pipeline_name, status, last_success_at, source_high_watermark,
              dirty_from_date, dirty_to_date, last_run_id, metadata, updated_at
            ) VALUES (
              %s, %s, CASE WHEN %s = 'ok' THEN now() END, %s, %s, %s, %s, %s, now()
            )
            ON CONFLICT (pipeline_name) DO UPDATE SET
              status = EXCLUDED.status,
              last_success_at = CASE
                WHEN EXCLUDED.status = 'ok' THEN now()
                ELSE ops.pipeline_watermarks.last_success_at
              END,
              source_high_watermark = CASE
                WHEN EXCLUDED.status = 'ok' THEN EXCLUDED.source_high_watermark
                ELSE ops.pipeline_watermarks.source_high_watermark
              END,
              dirty_from_date = EXCLUDED.dirty_from_date,
              dirty_to_date = EXCLUDED.dirty_to_date,
              last_run_id = COALESCE(EXCLUDED.last_run_id, ops.pipeline_watermarks.last_run_id),
              metadata = ops.pipeline_watermarks.metadata || EXCLUDED.metadata,
              updated_at = now()
            """,
            (
                pipeline_name, status, status, source_high_watermark,
                dirty_from_date, dirty_to_date, run_id, Jsonb(metadata or {}),
            ),
        )


def log_path(task_name: str) -> Path:
    now = dt.datetime.now(TZ)
    directory = ROOT / "logs/pipeline" / now.strftime("%Y%m%d")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{task_name}_{now.strftime('%H%M%S')}.log"


def run_logged(args: list[str], log_file: Path, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    cwd = cwd or ROOT
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as log:
        log.write(f"$ {' '.join(args)}\n")
        log.flush()
        proc = subprocess.Popen(
            args,
            cwd=str(cwd),
            env={**os.environ, **(env or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            log.write(line)
            sys.stdout.flush()
            log.flush()
        code = proc.wait()
        if code:
            raise subprocess.CalledProcessError(code, args)


@contextmanager
def crawler_run(
    task_name: str,
    source_name: str,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    log_file: Path | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    ensure_ops_schema()
    recovered = recover_stale_crawler_runs([task_name])
    if recovered:
        print(f"[crawler_run] recovered stale runs task={task_name} rows={recovered}")
    with connect(admin=False) as conn:
        row = conn.execute(
            """
            INSERT INTO ops.crawler_runs
              (task_name, source_name, status, start_date, end_date, log_path, metadata)
            VALUES (%s, %s, 'running', %s, %s, %s, %s)
            RETURNING id
            """,
            (
                task_name,
                source_name,
                start_date,
                end_date,
                str(log_file) if log_file else None,
                Jsonb(metadata or {}),
            ),
        ).fetchone()
        conn.commit()
        run_id = row["id"]
    state = {"id": run_id, "new_rows": None}
    try:
        yield state
    except Exception as exc:
        try:
            _finalize_crawler_run(run_id, "failed", state.get("new_rows"), str(exc))
        except Exception as state_exc:  # noqa: BLE001
            print(f"[crawler_run] failed to finalize failed run {run_id}: {state_exc}", file=sys.stderr)
        raise
    else:
        try:
            _finalize_crawler_run(run_id, "ok", state.get("new_rows"))
        except Exception as state_exc:  # noqa: BLE001
            print(f"[crawler_run] work completed but run status update failed for {run_id}: {state_exc}", file=sys.stderr)


def max_news_date(source_name: str) -> dt.date | None:
    with connect(admin=False) as conn:
        row = conn.execute(
            "SELECT max(newspaper_date) AS max_date FROM news.articles WHERE source_name = %s",
            (source_name,),
        ).fetchone()
    return row["max_date"] if row and row["max_date"] else None


def max_report_date() -> dt.date | None:
    with connect(admin=False) as conn:
        row = conn.execute("SELECT max(report_date) AS max_date FROM research.reports").fetchone()
    return row["max_date"] if row and row["max_date"] else None


def latest_pending_retry_plan() -> dict[str, Any] | None:
    with connect(admin=False) as conn:
        row = conn.execute(
            """
            SELECT id, plan
            FROM ops.retry_plans
            WHERE status = 'pending'
            ORDER BY plan_date DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    if not row:
        return None
    return {"id": row["id"], "plan": row["plan"]}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

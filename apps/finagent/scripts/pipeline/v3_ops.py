from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import datetime as dt
import os
import traceback
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from psycopg.types.json import Jsonb

from common import connect


QUALITY_VALUES = {"unknown", "pass", "warn", "fail", "degraded"}


def require_v3_schema() -> None:
    with connect(admin=False) as conn:
        row = conn.execute(
            "SELECT to_regclass('ops.dataset_watermarks') IS NOT NULL AS ready"
        ).fetchone()
    if not row or not row["ready"]:
        raise RuntimeError(
            "Finance Agent V3 schema is not installed; apply "
            "docker/postgres/migrate_finance_agent_v3.sql first"
        )


def acquire_controller_lock(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired",
        (f"finance-v3:{name}",),
    ).fetchone()
    return bool(row and row["acquired"])


def release_controller_lock(conn, name: str) -> None:
    conn.execute(
        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
        (f"finance-v3:{name}",),
    )


@contextmanager
def stage_run(
    pipeline_name: str,
    stage_name: str,
    *,
    expected_date: dt.date | None = None,
    scheduled_for: dt.datetime | None = None,
    input_watermarks: dict[str, Any] | None = None,
    log_path: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    require_v3_schema()
    run_id = uuid.uuid4()
    runtime_metadata = {
        **(metadata or {}),
        "process_pid": os.getpid(),
        "controller_parent_run_id": os.getenv("FINANCE_CONTROLLER_PARENT_RUN_ID") or None,
    }
    with connect(admin=False) as conn:
        conn.execute(
            """
            INSERT INTO ops.pipeline_runs_v3 (
              run_id,pipeline_name,stage_name,scheduled_for,expected_date,status,
              started_at,heartbeat_at,input_watermarks,log_path,metadata
            ) VALUES (%s,%s,%s,%s,%s,'running',now(),now(),%s,%s,%s)
            """,
            (
                run_id,
                pipeline_name,
                stage_name,
                scheduled_for,
                expected_date,
                Jsonb(input_watermarks or {}),
                log_path,
                Jsonb(runtime_metadata),
            ),
        )
    state: dict[str, Any] = {
        "run_id": run_id,
        "quality_status": "unknown",
        "output_watermarks": {},
        "metadata": {},
    }
    try:
        yield state
    except Exception as exc:
        with connect(admin=False) as conn:
            conn.execute(
                """
                UPDATE ops.pipeline_runs_v3
                SET status='failed',quality_status=CASE
                      WHEN quality_status='unknown' THEN 'fail' ELSE quality_status END,
                    completed_at=now(),heartbeat_at=now(),error_class=%s,error_message=%s,
                    output_watermarks=%s,metadata=metadata || %s
                WHERE run_id=%s
                """,
                (
                    type(exc).__name__,
                    str(exc)[:4000],
                    Jsonb(state.get("output_watermarks") or {}),
                    Jsonb({
                        **(state.get("metadata") or {}),
                        "traceback_tail": traceback.format_exc()[-4000:],
                    }),
                    run_id,
                ),
            )
        raise
    else:
        quality = str(state.get("quality_status") or "unknown")
        if quality not in QUALITY_VALUES:
            raise ValueError(f"invalid quality status: {quality}")
        with connect(admin=False) as conn:
            conn.execute(
                """
                UPDATE ops.pipeline_runs_v3
                SET status='succeeded',quality_status=%s,completed_at=now(),heartbeat_at=now(),
                    output_watermarks=%s,metadata=metadata || %s
                WHERE run_id=%s
                """,
                (
                    quality,
                    Jsonb(state.get("output_watermarks") or {}),
                    Jsonb(state.get("metadata") or {}),
                    run_id,
                ),
            )


def heartbeat(run_id: uuid.UUID) -> None:
    with connect(admin=False) as conn:
        conn.execute(
            "UPDATE ops.pipeline_runs_v3 SET heartbeat_at=now() WHERE run_id=%s AND status='running'",
            (run_id,),
        )


def record_quality(
    run_id: uuid.UUID,
    dataset_key: str,
    check_name: str,
    *,
    status: str,
    severity: str,
    partition_key: str = "default",
    observed_value: float | None = None,
    threshold_value: float | None = None,
    affected_rows: int | None = None,
    sample: list[dict[str, Any]] | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    if status not in {"pass", "warn", "fail"}:
        raise ValueError(f"invalid quality result status: {status}")
    if severity not in {"info", "warning", "error"}:
        raise ValueError(f"invalid quality severity: {severity}")
    with connect(admin=False) as conn:
        conn.execute(
            """
            INSERT INTO ops.quality_results_v3 (
              run_id,dataset_key,partition_key,check_name,severity,status,
              observed_value,threshold_value,affected_rows,sample,details
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                run_id,
                dataset_key,
                partition_key,
                check_name,
                severity,
                status,
                observed_value,
                threshold_value,
                affected_rows,
                Jsonb(sample or []),
                Jsonb(details or {}),
            ),
        )


def set_dataset_watermark(
    dataset_key: str,
    *,
    schema_version: str,
    run_id: uuid.UUID | None,
    partition_key: str = "default",
    expected_date: dt.date | None = None,
    data_through: dt.datetime | None = None,
    row_count: int | None = None,
    eligible_count: int | None = None,
    coverage_pct: float | None = None,
    quality_status: str = "unknown",
    source_version: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    if quality_status not in QUALITY_VALUES:
        raise ValueError(f"invalid watermark quality status: {quality_status}")
    with connect(admin=False) as conn:
        conn.execute(
            """
            INSERT INTO ops.dataset_watermarks (
              dataset_key,partition_key,expected_date,data_through,row_count,
              eligible_count,coverage_pct,quality_status,schema_version,
              source_version,run_id,completed_at,details
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s)
            ON CONFLICT (dataset_key,partition_key) DO UPDATE SET
              expected_date=EXCLUDED.expected_date,
              data_through=EXCLUDED.data_through,
              row_count=EXCLUDED.row_count,
              eligible_count=EXCLUDED.eligible_count,
              coverage_pct=EXCLUDED.coverage_pct,
              quality_status=EXCLUDED.quality_status,
              schema_version=EXCLUDED.schema_version,
              source_version=EXCLUDED.source_version,
              run_id=EXCLUDED.run_id,
              completed_at=now(),
              details=EXCLUDED.details
            """,
            (
                dataset_key,
                partition_key,
                expected_date,
                data_through,
                row_count,
                eligible_count,
                coverage_pct,
                quality_status,
                schema_version,
                source_version,
                run_id,
                Jsonb(details or {}),
            ),
        )
        conn.execute(
            """
            INSERT INTO analytics.dataset_coverage_latest (
              dataset_key,partition_key,data_through,row_count,entity_count,
              eligible_count,coverage_pct,quality_status,details,refreshed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
            ON CONFLICT (dataset_key,partition_key) DO UPDATE SET
              data_through=EXCLUDED.data_through,row_count=EXCLUDED.row_count,
              entity_count=EXCLUDED.entity_count,eligible_count=EXCLUDED.eligible_count,
              coverage_pct=EXCLUDED.coverage_pct,quality_status=EXCLUDED.quality_status,
              details=EXCLUDED.details,refreshed_at=now()
            """,
            (
                dataset_key,
                partition_key,
                data_through,
                row_count,
                row_count,
                eligible_count,
                coverage_pct,
                quality_status,
                Jsonb(details or {}),
            ),
        )


def get_watermarks(dataset_keys: list[str]) -> dict[str, dict[str, Any]]:
    if not dataset_keys:
        return {}
    with connect(admin=False) as conn:
        rows = conn.execute(
            """
            SELECT * FROM ops.dataset_watermarks
            WHERE dataset_key=ANY(%s)
            ORDER BY dataset_key,partition_key
            """,
            (dataset_keys,),
        ).fetchall()
    return {f"{row['dataset_key']}:{row['partition_key']}": dict(row) for row in rows}

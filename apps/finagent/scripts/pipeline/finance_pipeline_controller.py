#!/usr/bin/env python3
from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from common import ROOT, TZ, connect
from v3_ops import acquire_controller_lock, stage_run


CommandFactory = Callable[[dt.date], list[list[str]]]


@dataclass(frozen=True)
class Stage:
    name: str
    earliest: dt.time
    commands: CommandFactory
    dependencies: tuple[str, ...] = ()
    weekdays_only: bool = False
    active_only: bool = False
    allow_terminal_dependencies: tuple[str, ...] = ()
    external_ready: Callable[[dt.date], tuple[bool, str]] | None = None
    catchup: bool = True


def commands(*items: list[str]) -> CommandFactory:
    return lambda _date: list(items)


def date_commands(factory: Callable[[dt.date], list[list[str]]]) -> CommandFactory:
    return factory


def market_ready(run_date: dt.date) -> tuple[bool, str]:
    with connect(admin=False) as conn:
        running = conn.execute(
            """
            SELECT stage_name FROM ops.pipeline_runs_v3
            WHERE status='running' AND heartbeat_at > now()-interval '4 hours'
              AND stage_name IN ('asset_lifecycle','canonical_daily_v2','adjustment_factors_v3')
            ORDER BY stage_name
            """
        ).fetchall()
        if running:
            return False, "v3 market jobs already running:" + ",".join(row["stage_name"] for row in running)
        row = conn.execute(
            """
            SELECT count(DISTINCT b.asset_id) AS assets
            FROM market.bars_daily b
            JOIN market.assets a ON a.asset_id=b.asset_id
            WHERE b.ts >= (%s::date::timestamp AT TIME ZONE 'Asia/Shanghai')
              AND b.ts < ((%s::date+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
              AND a.asset_type='stock'
            """,
            (run_date, run_date),
        ).fetchone()
    assets = int(row["assets"] or 0)
    return assets >= 3000, f"legacy market rows ready={assets}/3000"


def v3_morning_jobs_idle(_run_date: dt.date) -> tuple[bool, str]:
    with connect(admin=False) as conn:
        rows = conn.execute(
            """
            SELECT stage_name FROM ops.pipeline_runs_v3
            WHERE status='running' AND heartbeat_at > now()-interval '4 hours'
              AND stage_name IN (
                'cninfo_filings','corporate_actions_cninfo',
                'financial_facts_cninfo','document_quality'
              )
            ORDER BY stage_name
            """
        ).fetchall()
    names = [row["stage_name"] for row in rows]
    return not names, "v3 jobs already running:" + ",".join(names)


def report_command(run_date: dt.date) -> list[list[str]]:
    cmd = [
        sys.executable, "scripts/pipeline/daily_ai_hardware_report_v3.py",
        "--date", run_date.isoformat(),
    ]
    if os.getenv("FINANCE_V3_PUBLISH", "0").lower() in {"1", "true", "yes"}:
        cmd.append("--publish")
    return [cmd]


def outbox_command(run_date: dt.date) -> list[list[str]]:
    if os.getenv("FINANCE_DAILY_DELIVERY", "file") != "outbox" or os.getenv("FINANCE_V3_PUBLISH", "0").lower() not in {"1", "true", "yes"}:
        return []
    return [[
        sys.executable, "scripts/pipeline/enqueue_daily_report_v3.py",
        "--date", run_date.isoformat(),
    ]]


def earnings_observation_commands(run_date: dt.date) -> list[list[str]]:
    # XtraStock bootstrap sets the configured state path. Legacy installations
    # keep their established application-local log/state directory.
    state=Path(os.environ.get('XTRASTOCK_STATE_DIR',str(ROOT/'logs/state')))
    target=state/'earnings-observations'/run_date.isoformat()
    return [[sys.executable,'scripts/analytics/collect_earnings_observations.py',
        '--start',(run_date-dt.timedelta(days=180)).isoformat(),'--end',run_date.isoformat(),
        '--output',str(target),'--apply']]


def daily_plan_commands(run_date: dt.date) -> list[list[str]]:
    return [[sys.executable,'scripts/analytics/run_daily_value_plan.py','--date',run_date.isoformat(),'--apply','--prepare-for-report']]


def market_v2_commands(run_date: dt.date) -> list[list[str]]:
    value = run_date.isoformat()
    return [
        [
            sys.executable, "scripts/analytics/sync_asset_lifecycle_v3.py",
            "--as-of", value,
        ],
        [
            sys.executable, "scripts/analytics/sync_canonical_daily_bars_v2.py",
            "--since", value, "--until", value, "--allow-quality-fail",
        ],
        [
            sys.executable, "scripts/analytics/build_adjustment_factors_v3.py",
            "--since", value, "--until", value, "--allow-quality-fail",
        ],
    ]


def nightly_v3_commands(run_date: dt.date) -> list[list[str]]:
    from market_data_date import complete_market_date
    with connect() as conn:
        data_date,_=complete_market_date(conn,run_date)
    return [
        [
            sys.executable, "scripts/analytics/materialize_financial_facts_v3.py",
            "--since", (run_date - dt.timedelta(days=14)).isoformat(),
            "--until", run_date.isoformat(),
        ],
        [
            sys.executable, "scripts/analytics/refresh_v3_preaggregates.py",
            "--as-of", data_date.isoformat(),
        ],
        [
            sys.executable, "scripts/analytics/refresh_watchlist_outcomes.py",
        ],
        [
            sys.executable, "scripts/search/score_document_quality_v3.py",
            "--batch-size", "1000",
        ],
    ]


def quality_gate_commands(_run_date: dt.date) -> list[list[str]]:
    return [
        [sys.executable,"scripts/analytics/build_research_snapshot.py"],
        [
            sys.executable, "scripts/search/score_document_quality_v3.py",
            "--batch-size", "1000",
        ],
        [
            sys.executable, "scripts/analytics/refresh_v3_preaggregates.py",
        ],
        [
            sys.executable, "scripts/pipeline/evaluate_data_gate_v3.py",
        ],
    ]


SHADOW_STAGES = (
    Stage(
        "morning_v3_sources", dt.time(8, 0),
        commands(
            [
                sys.executable, "scripts/pipeline/daily_cninfo_filings.py",
                "--lookback-days", "3", "--download-financial-pdfs",
                "--download-corporate-action-pdfs",
            ],
            [
                sys.executable, "scripts/analytics/extract_corporate_actions_cninfo.py",
                "--lookback-days", "14",
            ],
            [
                sys.executable, "scripts/analytics/extract_financial_facts_cninfo.py",
                "--lookback-days", "14",
            ],
            [sys.executable, "scripts/search/score_document_quality_v3.py", "--batch-size", "1000"],
        ), external_ready=v3_morning_jobs_idle,
    ),
    Stage("morning_quality_gate", dt.time(8, 47), quality_gate_commands, ("morning_v3_sources",)),
    Stage("daily_value_plan", dt.time(8, 49), daily_plan_commands, ("morning_quality_gate",), weekdays_only=True),
    Stage("daily_report_v3", dt.time(8, 50), report_command, ("daily_value_plan",), weekdays_only=True),
    Stage("daily_outbox_v3", dt.time(9, 5), outbox_command, ("daily_report_v3",), weekdays_only=True),
    Stage(
        "market_v2_daily", dt.time(17, 10), market_v2_commands,
        weekdays_only=True, external_ready=market_ready,
    ),
    Stage("nightly_v3_preaggregates", dt.time(23, 0), nightly_v3_commands, ("market_v2_daily",), weekdays_only=True),
)


def research_rebuild_idle(_run_date):
    """Do not train from a partially rebuilt market range; observe the real unit."""
    result=subprocess.run(['systemctl','--user','show','finance-quant-market-finalizer.service',
        '--property=ActiveState','--value'],capture_output=True,text=True,timeout=5)
    if result.returncode:
        return False,'cannot verify market rebuild service state'
    state=result.stdout.strip()
    return state == 'inactive', 'market rebuild service: '+state


ACTIVE_STAGES = (
    Stage('active_targeted_retry',dt.time(7,30),
        commands([sys.executable,'scripts/pipeline/run_retry_plan.py','--skip-market']),active_only=True),
    Stage(
        "active_news", dt.time(8, 0),
        commands([sys.executable, "scripts/pipeline/daily_news_incremental.py"]),
        ("active_targeted_retry",), active_only=True, allow_terminal_dependencies=("active_targeted_retry",),
    ),
    Stage("morning_research_focus",dt.time(8,40),
        date_commands(lambda d:[[sys.executable,"scripts/pipeline/research_intelligence_refresh.py",
            "--date",d.isoformat(),"--scope","focus"]]),("active_news",)),
    Stage("morning_quality_gate", dt.time(8, 47), quality_gate_commands, ("active_news",)),
    Stage("daily_value_plan", dt.time(8, 49), daily_plan_commands, ("morning_quality_gate",), weekdays_only=True),
    Stage("daily_report_v3", dt.time(8, 50), report_command, ("daily_value_plan",), weekdays_only=True),
    Stage("daily_outbox_v3", dt.time(9, 5), outbox_command, ("daily_report_v3",), weekdays_only=True),
    Stage(
        "active_market_close", dt.time(17, 5),
        date_commands(lambda d: [[sys.executable, "scripts/pipeline/daily_market_close.py", "--workers", "4", "--date", d.isoformat()]]),
        weekdays_only=True, active_only=True, external_ready=research_rebuild_idle,
    ),
    Stage(
        "active_reports", dt.time(18, 0),
        date_commands(lambda d: [[sys.executable, "scripts/pipeline/daily_reports_incremental.py", "--end-date", d.isoformat()]]),
        active_only=True,
    ),
    Stage('earnings_observations',dt.time(18,20),earnings_observation_commands,('active_reports',),active_only=True,catchup=False),
    Stage('daily_value_plan_settlement',dt.time(20,25),
        date_commands(lambda d:[[sys.executable,'scripts/analytics/settle_value_plan_paper.py','--date',d.isoformat()]]),
        ('active_market_close',),weekdays_only=True,active_only=True,external_ready=research_rebuild_idle),
    Stage(
        "active_analytics", dt.time(20, 5),
        date_commands(lambda d: [[sys.executable, "scripts/pipeline/analytics_core_refresh.py", "--date", d.isoformat()]]),
        ("active_market_close", "active_reports"), weekdays_only=True, active_only=True,
        allow_terminal_dependencies=("active_market_close",), external_ready=research_rebuild_idle,
    ),
    Stage(
        "active_graph", dt.time(21, 0),
        date_commands(lambda d: [[sys.executable, "scripts/pipeline/knowledge_graph_refresh.py", "--date", d.isoformat()]]),
        ("active_analytics",), weekdays_only=True, active_only=True, external_ready=research_rebuild_idle,
    ),
    Stage(
        "active_factors", dt.time(22, 30),
        date_commands(lambda d: [[sys.executable, "scripts/pipeline/factor_daily_refresh.py", "--date", d.isoformat()]]),
        ("active_graph",), weekdays_only=True, active_only=True, external_ready=research_rebuild_idle,
    ),
    Stage("research_snapshot", dt.time(22, 30),
        date_commands(lambda d: [[sys.executable, "scripts/analytics/build_research_snapshot.py", "--date", d.isoformat()]]),
        ("active_factors",), weekdays_only=True, external_ready=research_rebuild_idle),
    Stage("research_intelligence",dt.time(22,40),
        date_commands(lambda d:[[sys.executable,"scripts/pipeline/research_intelligence_refresh.py",
            "--date",d.isoformat()]]),("active_reports",),external_ready=research_rebuild_idle),
    Stage("nightly_v3_preaggregates", dt.time(22, 30), nightly_v3_commands,
        ("research_snapshot",), weekdays_only=True),
    Stage(
        "active_nightly_verify", dt.time(23, 30),
        commands([sys.executable, "scripts/pipeline/nightly_verify.py"]),
        ("nightly_v3_preaggregates", "active_reports"), active_only=True,
    ),
)


def log(event: str, **payload) -> None:
    print(json.dumps({
        "ts": dt.datetime.now(TZ).isoformat(), "service": "finance-pipeline-controller",
        "event": event, **payload,
    }, ensure_ascii=False, default=str), flush=True)


def successful_stages(run_date: dt.date, mode: str = "active") -> set[str]:
    with connect(admin=False) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT stage_name FROM ops.pipeline_runs_v3
            WHERE pipeline_name='finance_pipeline_controller' AND expected_date=%s
              AND status='succeeded' AND metadata->>'mode'=%s
            """,
            (run_date, mode),
        ).fetchall()
    return {row["stage_name"] for row in rows}


def last_failure(stage_name: str, run_date: dt.date, mode: str = "active") -> dt.datetime | None:
    with connect(admin=False) as conn:
        row = conn.execute(
            """
            SELECT completed_at FROM ops.pipeline_runs_v3
            WHERE pipeline_name='finance_pipeline_controller' AND stage_name=%s
              AND expected_date=%s AND status='failed' AND metadata->>'mode'=%s
            ORDER BY completed_at DESC LIMIT 1
            """,
            (stage_name, run_date, mode),
        ).fetchone()
    return row["completed_at"] if row else None


def failure_count(stage_name, run_date, mode="active"):
    with connect(admin=False) as conn:
        return int(conn.execute("""SELECT count(*) n FROM ops.pipeline_runs_v3
            WHERE pipeline_name='finance_pipeline_controller' AND stage_name=%s
              AND expected_date=%s AND status='failed' AND metadata->>'mode'=%s""",(stage_name,run_date,mode)).fetchone()["n"])


def pending_dates(now, mode):
    # Resume evenings already started by this controller, including after a
    # restart. Never initiate old morning publication or an entire historical crawl.
    with connect(admin=False) as conn:
        rows=conn.execute("""SELECT DISTINCT expected_date FROM ops.pipeline_runs_v3
            WHERE pipeline_name='finance_pipeline_controller' AND metadata->>'mode'=%s
              AND expected_date BETWEEN %s::date-2 AND %s::date-1
              AND stage_name IN ('active_market_close','active_reports','active_analytics',
                'active_graph','active_factors','research_snapshot','research_intelligence','nightly_v3_preaggregates','market_v2_daily')
            ORDER BY expected_date""",(mode,now.date(),now.date())).fetchall()
    return [r['expected_date'] for r in rows]+[now.date()]


def stage_due(stage, run_date, now):
    if run_date>now.date() or (stage.weekdays_only and run_date.weekday()>=5):
        return False
    if run_date<now.date():
        return stage.catchup and stage.earliest>=dt.time(17) and (now.date()-run_date).days<=2
    return now.time().replace(tzinfo=None)>=stage.earliest


def missing_dependencies(stage, run_date, succeeded, stages, terminal):
    by_name={item.name:item for item in stages}
    return [dep for dep in stage.dependencies if dep not in succeeded
        and not (run_date.weekday()>=5 and by_name[dep].weekdays_only)
        and not (dep in stage.allow_terminal_dependencies and dep in terminal)]


def run_stage(stage: Stage, run_date: dt.date, mode: str) -> None:
    command_list = stage.commands(run_date)
    with stage_run(
        "finance_pipeline_controller", stage.name, expected_date=run_date,
        metadata={"mode": mode, "commands": command_list},
    ) as run:
        for command in command_list:
            log("command_started", stage=stage.name, command=command)
            started = time.monotonic()
            child_env = os.environ.copy()
            child_env["FINANCE_CONTROLLER_PARENT_RUN_ID"] = str(run["run_id"])
            result = subprocess.run(command, cwd=ROOT, check=False, env=child_env)
            if result.returncode:
                raise subprocess.CalledProcessError(result.returncode, command)
            log("command_completed", stage=stage.name, duration_seconds=round(time.monotonic() - started, 3))
        quality_rank = {"unknown": 0, "pass": 1, "warn": 2, "degraded": 3, "fail": 4}
        with connect(admin=False) as conn:
            children = conn.execute(
                """
                SELECT pipeline_name,stage_name,status,quality_status,run_id
                FROM ops.pipeline_runs_v3
                WHERE metadata->>'controller_parent_run_id'=%s
                ORDER BY started_at
                """,
                (str(run["run_id"]),),
            ).fetchall()
            report_status = None
            if stage.name == "daily_report_v3":
                report_row = conn.execute(
                    """
                    SELECT status FROM agent.report_runs
                    WHERE report_date=%s
                    ORDER BY completed_at DESC NULLS LAST,created_at DESC LIMIT 1
                    """,
                    (run_date,),
                ).fetchone()
                report_status = report_row["status"] if report_row else None
        qualities = [row["quality_status"] for row in children]
        if report_status in {"degraded", "quarantined", "failed"}:
            qualities.append("degraded" if report_status == "degraded" else "fail")
        run["quality_status"] = max(qualities or ["pass"], key=lambda value: quality_rank.get(value, 0))
        run["metadata"] = {
            "mode": mode,
            "command_count": len(command_list),
            "child_runs": [str(row["run_id"]) for row in children],
            "child_quality": qualities,
            "report_status": report_status,
        }


def recover_orphaned_controller_runs() -> int:
    with connect(admin=False) as conn:
        parents = conn.execute(
            """
            UPDATE ops.pipeline_runs_v3
            SET status='failed',quality_status='fail',completed_at=now(),heartbeat_at=now(),
                error_class='ControllerRestart',
                error_message='controller restarted before stage completion'
            WHERE pipeline_name='finance_pipeline_controller' AND status='running'
            RETURNING run_id
            """
        ).fetchall()
        children = conn.execute(
            """
            UPDATE ops.pipeline_runs_v3 child
            SET status='failed',quality_status='fail',completed_at=now(),heartbeat_at=now(),
                error_class='OrphanedChild',
                error_message='parent controller run is no longer active'
            WHERE child.status='running'
              AND nullif(child.metadata->>'controller_parent_run_id','') IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM ops.pipeline_runs_v3 parent
                WHERE parent.run_id=(child.metadata->>'controller_parent_run_id')::uuid
                  AND parent.status='running'
              )
            RETURNING child.run_id
            """
        ).fetchall()
    return len(parents) + len(children)


def main() -> int:
    mode = os.getenv("FINANCE_V3_CONTROLLER_MODE", "shadow").strip().lower()
    if mode not in {"shadow", "active"}:
        raise SystemExit("FINANCE_V3_CONTROLLER_MODE must be shadow or active")
    poll_seconds = max(10, int(os.getenv("FINANCE_V3_CONTROLLER_POLL_SECONDS", "30")))
    retry_minutes = max(5, int(os.getenv("FINANCE_V3_CONTROLLER_RETRY_MINUTES", "20")))
    max_attempts = max(1, int(os.getenv("FINANCE_CONTROLLER_MAX_ATTEMPTS", "2")))
    stop = False

    def stop_handler(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    lock_conn = connect(admin=False)
    lock_conn.autocommit = True
    if not acquire_controller_lock(lock_conn, "controller"):
        log("duplicate_instance_rejected")
        return 0
    recovered = recover_orphaned_controller_runs()
    stages = ACTIVE_STAGES if mode == "active" else SHADOW_STAGES
    waiting_state: dict[str, str] = {}
    log("controller_started", mode=mode, publish=os.getenv("FINANCE_V3_PUBLISH", "0"), recovered_runs=recovered)
    try:
        while not stop:
            now = dt.datetime.now(TZ)
            for run_date in pending_dates(now,mode):
                succeeded = successful_stages(run_date,mode)
                for stage in stages:
                    if stop:
                        break
                    if stage.name in succeeded:
                        continue
                    now=dt.datetime.now(TZ)
                    if not stage_due(stage,run_date,now):
                        continue
                    terminal={dep for dep in stage.allow_terminal_dependencies
                              if failure_count(dep,run_date,mode)>=max_attempts}
                    missing_deps=missing_dependencies(stage,run_date,succeeded,stages,terminal)
                    reason = ""
                    if missing_deps:
                        reason = "dependencies:" + ",".join(missing_deps)
                    elif stage.external_ready:
                        ready, detail = stage.external_ready(run_date)
                        if not ready:
                            reason = detail
                    failure_at = last_failure(stage.name, run_date,mode)
                    if failure_count(stage.name, run_date,mode) >= max_attempts:
                        reason = "attempt_budget_exhausted; manual inspection required"
                    if not reason and failure_at:
                        failure_local = failure_at.astimezone(TZ)
                        if now < failure_local + dt.timedelta(minutes=retry_minutes):
                            reason = f"retry_backoff_until:{(failure_local + dt.timedelta(minutes=retry_minutes)).isoformat()}"
                    if reason:
                        if waiting_state.get(stage.name) != reason:
                            log("stage_waiting", stage=stage.name, run_date=str(run_date), reason=reason)
                            waiting_state[stage.name] = reason
                        continue
                    waiting_state.pop(stage.name, None)
                    try:
                        log("stage_started", stage=stage.name, run_date=str(run_date), mode=mode)
                        run_stage(stage, run_date, mode)
                        succeeded.add(stage.name)
                        log("stage_completed", stage=stage.name, run_date=str(run_date))
                    except Exception as exc:  # noqa: BLE001
                        log("stage_failed", stage=stage.name, run_date=str(run_date), error_class=type(exc).__name__, error=str(exc))
            for _ in range(poll_seconds):
                if stop:
                    break
                time.sleep(1)
    finally:
        try:
            lock_conn.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s,0))",
                ("finance-v3:controller",),
            )
        finally:
            lock_conn.close()
    log("controller_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run deterministic daily finance-agent quality gates and persist one report."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import time
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GROUPED_COMMANDS = [
    [sys.executable, "-m", "unittest", "tests.test_embedding_batches", "tests.test_embedding_outage", "tests.test_paper_execution", "tests.test_quant_readiness", "tests.test_paper_targets", "tests.test_repair_windows", "tests.test_snapshot_input_contract", "tests.test_retrieval_bounded", "-v"],
    [sys.executable, "-m", "unittest", "tests.test_financial_factor_features", "tests.test_factor_input_contract", "tests.test_factor_validation", "tests.test_factor_production", "tests.test_research_upgrade", "tests.test_market_identity_gap", "tests.test_security_master_evidence", "tests.test_quant_market_finalizer", "tests.test_weekly_factor_gate", "tests.test_factor_governance_tools", "tests.test_snapshot_factor_governance", "tests.test_canonical_lifecycle_sql", "tests.test_nontrading_gap_evidence", "-v"],
    [sys.executable, "-m", "unittest", "tests.test_graph_revision_v5.CutoffTests", "tests.test_graph_source_review.SourceReviewTests", "tests.test_research_focus", "tests.test_forecast_learning", "tests.test_learning_input_reader", "tests.test_learning_failure_display", "tests.test_learning_monitor", "tests.test_research_focus_feedback", "tests.test_research_intelligence_contract", "tests.test_graph_smoke_contract", "-v"],
    [sys.executable, "-m", "unittest", "tests.test_report_evidence_analysis", "tests.test_reader_daily_language", "tests.test_daily_reasoning_contract", "tests.test_controller_upgrade", "tests.test_research_intelligence_runner", "-v"],
    [sys.executable, "-m", "unittest", "MMClaw.tests.test_document_semantic_search", "MMClaw.tests.test_finance_agent_quality", "MMClaw.tests.test_user_presentation", "MMClaw.tests.test_reader_language", "-v"],
    [sys.executable, "scripts/agent/smoke_finance_v3_tools.py"],
    [sys.executable, "scripts/agent/smoke_hybrid_retrieval.py"],
    [sys.executable, "tests/test_factor_score_scope_sql.py"],
    [sys.executable, "-m", "unittest", "tests.test_data_tools_repair", "tests.test_cninfo_summary_scopes", "tests.test_cninfo_statements_v3", "tests.test_company_evidence_scope", "tests.test_document_event_rule_store", "-v"],
    [sys.executable, "-m", "unittest", "tests.test_report_attachments", "tests.test_cninfo_body_acquisition", "-v"],
    [sys.executable, "-m", "unittest", "MMClaw.tests.test_goal_loop", "MMClaw.tests.test_goal_examples", "MMClaw.tests.test_agentic_loop", "MMClaw.tests.test_delivery_contract", "MMClaw.tests.test_tool_efficiency_time", "tests.test_analysis_delivery_acceptance", "-v"],
]


# Pipeline and analytics both still expose common.py. Every test module runs in
# a fresh process so sys.modules cannot bind the other subsystem's implementation.
def isolated_commands(commands):
    result = []
    for command in commands:
        if command[1:3] == ["-m", "unittest"]:
            result.extend([[command[0], "-m", "unittest", module, "-v"]
                           for module in command[3:] if not module.startswith("-")])
        else:
            result.append(command)
    return result

COMMANDS = isolated_commands(GROUPED_COMMANDS)
for module in (
    'test_data_registry_alignment','test_research_asset_registry','test_incremental_import',
    'test_report_body_import','test_report_body_recovery','test_forecast_semantics',
    'test_forecast_revision','test_research_security_aliases','test_preaggregate_freshness',
    'test_graph_writer_wait','test_graph_entity_refresh','test_graph_tool_bounded_history',
    'test_repository_ops','test_search_identity_index',
):
    COMMANDS.append([sys.executable, "-m", "unittest", "tests."+module, "-v"])

RETRYABLE_CHECKS = {
    "scripts/agent/smoke_finance_v3_tools.py",
    "scripts/agent/smoke_hybrid_retrieval.py",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unit-only', action='store_true', help='Skip live database/tool probes.')
    parser.add_argument('--output-dir', type=Path, default=ROOT/'logs/agent_quality')
    args = parser.parse_args()
    selected = [c for c in COMMANDS if not args.unit_only or c[1:3] == ['-m','unittest']]
    rows = []
    for command in selected:
        command = [sys.executable, *command[1:]]
        env = {"PYTHONPATH": str(ROOT)+":"+str(ROOT / "MMClaw")+":"+str(ROOT.parents[1]/"src")}
        attempts = []
        max_attempts = 2 if any(part in RETRYABLE_CHECKS for part in command) else 1
        for attempt in range(1, max_attempts + 1):
            try:
                proc = subprocess.run(
                    command, cwd=ROOT, capture_output=True, text=True, timeout=300,
                    env={**__import__("os").environ, **env},
                )
            except subprocess.TimeoutExpired as exc:
                output=exc.stdout or ''
                if isinstance(output,bytes):output=output.decode(errors='replace')
                proc=subprocess.CompletedProcess(command,124,output,'quality check exceeded 300 seconds')
            attempts.append({
                "attempt": attempt,
                "returncode": proc.returncode,
                "stdout_tail": proc.stdout[-3000:],
                "stderr_tail": proc.stderr[-3000:],
            })
            if proc.returncode == 0 or attempt == max_attempts:
                break
            time.sleep(3)
        rows.append({
            "command": command, "passed": proc.returncode == 0, "returncode": proc.returncode,
            "stdout_tail": proc.stdout[-3000:], "stderr_tail": proc.stderr[-3000:],
            "attempt_count": len(attempts), "attempts": attempts,
        })
        if proc.returncode and args.unit_only:
            print('FAILED CHECK: ' + ' '.join(command), flush=True)
            print(proc.stdout[-3000:] + proc.stderr[-3000:], flush=True)
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    report = {
        "generated_at": now.isoformat(), "passed": all(row["passed"] for row in rows),
        "checks_passed": sum(row["passed"] for row in rows), "checks_total": len(rows), "checks": rows, "isolated_test_modules": True, "live_probes_included": not args.unit_only,
    }
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"quality_gate_{now.strftime('%Y%m%d_%H%M%S')}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "latest_quality_gate.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "checks_passed": report["checks_passed"], "checks_total": len(rows), "output": str(output)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

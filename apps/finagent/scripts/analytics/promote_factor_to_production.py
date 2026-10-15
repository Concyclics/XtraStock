#!/usr/bin/env python3
"""Manually approve a shadow factor for production after hard readiness gates."""
from __future__ import annotations

import argparse
import datetime as dt
import json

from common import connect, jsonb
from factor_validation import independent_windows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--factor-name", required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--min-oos-windows", type=int, default=3)
    parser.add_argument("--min-coverage", type=float, default=0.80)
    parser.add_argument("--approve", action="store_true", help="Commit the transition. Without this flag, only audit readiness.")
    args = parser.parse_args()

    with connect(autocommit=False) as conn:
        factor = conn.execute(
            """SELECT factor_id,factor_name,version,lifecycle_status
               FROM factor.definitions WHERE factor_name=%s FOR UPDATE""",
            (args.factor_name,),
        ).fetchone()
        if not factor:
            raise RuntimeError(f"unknown factor: {args.factor_name}")
        if factor["lifecycle_status"] != "shadow":
            raise RuntimeError(f"factor must be shadow, got {factor['lifecycle_status']}")

        evaluations = conn.execute(
            """SELECT e.run_id,e.passed_gates,e.rank_ic_mean,e.rank_ic_ir,
                      e.top_bottom_spread_mean,e.turnover_mean,e.q_value,e.metrics,
                      r.test_start,r.test_end,r.horizon_days,r.status
               FROM factor.evaluations e
               JOIN factor.experiment_runs r USING(run_id)
               WHERE e.factor_id=%s AND e.sample_name='test'
                 AND r.config->>'validation_state' IS DISTINCT FROM 'superseded'
               ORDER BY r.started_at DESC""",
            (factor["factor_id"],),
        ).fetchall()
        passing_windows = independent_windows(evaluations, factor["version"])
        latest_eval = next((row for row in evaluations if row["status"] == "complete"), None)

        health = conn.execute(
            """SELECT h.*,m.latest_market_date
               FROM factor.factor_health_daily h
               CROSS JOIN (SELECT max(dt) AS latest_market_date FROM market.canonical_bars_daily) m
               WHERE h.factor_id=%s ORDER BY h.dt DESC LIMIT 1""",
            (factor["factor_id"],),
        ).fetchone()

        blockers: list[str] = []
        if len(passing_windows) < args.min_oos_windows:
            blockers.append(f"independent passing OOS windows {len(passing_windows)} < {args.min_oos_windows}")
        if not latest_eval or not independent_windows([latest_eval], factor["version"]):
            blockers.append("latest completed evaluation did not pass")
        if not health:
            blockers.append("factor health is missing")
        else:
            if float(health["coverage"] or 0) < args.min_coverage:
                blockers.append(f"latest coverage {health['coverage']:.3f} < {args.min_coverage:.3f}")
            if health["dt"] != health["latest_market_date"]:
                blockers.append(f"score date {health['dt']} != latest canonical market date {health['latest_market_date']}")
            if health["raw_stddev"] is None or float(health["raw_stddev"]) == 0:
                blockers.append("latest cross-sectional factor variance is zero")
            if health["zero_value_ratio"] is not None and float(health["zero_value_ratio"]) >= 0.99:
                blockers.append("latest factor is at least 99% zero")

        checklist = {
            "passing_oos_windows": len(passing_windows),
            "required_oos_windows": args.min_oos_windows,
            "latest_evaluation_run_id": str(latest_eval["run_id"]) if latest_eval else None,
            "latest_health_date": health["dt"].isoformat() if health else None,
            "latest_coverage": float(health["coverage"]) if health else None,
            "blockers": blockers,
        }
        print(json.dumps(checklist, ensure_ascii=False, default=str))
        if blockers:
            conn.rollback()
            raise RuntimeError("production readiness failed: " + "; ".join(blockers))
        if not args.approve:
            conn.rollback()
            print("readiness passed; rerun with --approve to commit")
            return 0

        run_id = latest_eval["run_id"]
        conn.execute(
            """INSERT INTO factor.production_approvals (
                 factor_id,factor_version,evaluation_run_id,decision,approved_by,
                 reason,effective_date,checklist
               ) VALUES (%s,%s,%s,'approved',%s,%s,%s,%s)""",
            (
                factor["factor_id"], factor["version"], run_id,
                args.approved_by, args.reason, dt.date.today(), jsonb(checklist),
            ),
        )
        conn.execute(
            "UPDATE factor.definitions SET lifecycle_status='production',updated_at=now() WHERE factor_id=%s",
            (factor["factor_id"],),
        )
        conn.execute(
            """INSERT INTO factor.lifecycle_events
                 (factor_id,from_status,to_status,run_id,reason,actor)
               VALUES (%s,'shadow','production',%s,%s,%s)""",
            (factor["factor_id"], run_id, args.reason, args.approved_by),
        )
        conn.commit()
    print(f"promoted factor={args.factor_name} to production")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

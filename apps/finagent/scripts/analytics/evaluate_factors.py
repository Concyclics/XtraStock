#!/usr/bin/env python3
"""Walk-forward evaluation, multiple-test correction, and optional promotion."""
from __future__ import annotations

import argparse
import datetime as dt
import math
import hashlib
import json
import subprocess
from typing import Any

try:
    from .common import connect, jsonb
    from .factor_dsl import FEATURES, compile_expression
    from .factor_validation import VALIDATION_VERSION, hac_p_value
    from .factor_input_contract import INPUT_CONTRACT_VERSION, require_stock_horizon
    from .lifecycle_evidence import lifecycle_order
except ImportError:  # Direct script execution keeps scripts/analytics on sys.path.
    from common import connect, jsonb
    from factor_dsl import FEATURES, compile_expression
    from factor_validation import VALIDATION_VERSION, hac_p_value
    from factor_input_contract import INPUT_CONTRACT_VERSION, require_stock_horizon
    from lifecycle_evidence import lifecycle_order


def p_value_from_mean(mean: float | None, stddev: float | None, count: int) -> float | None:
    if mean is None or stddev in (None, 0) or count < 2:
        return None
    statistic = abs(mean) / (stddev / math.sqrt(count))
    return math.erfc(statistic / math.sqrt(2.0))


def git_version() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def evaluate(
    cur,
    expression_sql: str,
    direction: int,
    start: dt.date,
    end: dt.date,
    horizon: int,
    label_cutoff: dt.date | None = None,
    round_trip_bps: float = 50.0,
) -> dict[str, Any]:
    require_stock_horizon(horizon)
    return_column = {1: "return_1d", 5: "return_5d", 20: "return_20d", 60: "return_60d"}[horizon]
    query = f"""
    WITH raw AS (
      SELECT f.asset_id, f.dt, ({expression_sql}) * %(direction)s AS factor_value,
             r.{return_column} AS forward_return, r.exit_date_{horizon}d AS outcome_end,
             (f.input_quality->>'strict_pit_ready'='true' AND life.quality_status='verified') AS strict_pit_ready,
             (entry.volume>0 AND entry.high>entry.low AND exit_bar.volume>0
              AND exit_bar.high>exit_bar.low
              AND entry.quality_status NOT IN ('rejected','conflict','incomplete')
              AND exit_bar.quality_status NOT IN ('rejected','conflict','incomplete')
              AND r.entry_date=(SELECT min(c.trade_date) FROM market.trading_calendar c
                  WHERE c.exchange='SSE' AND c.session_status='open' AND (c.is_verified OR c.source_name='observed market bars fallback')
                    AND c.trade_date>f.dt)
              AND (SELECT count(*) FROM market.trading_calendar c
                  WHERE c.exchange='SSE' AND c.session_status='open' AND (c.is_verified OR c.source_name='observed market bars fallback')
                    AND c.trade_date>f.dt AND c.trade_date<=r.exit_date_{horizon}d)=%(horizon)s
             ) AS executable
      FROM factor.feature_daily f
      JOIN factor.forward_returns r ON r.asset_id=f.asset_id AND r.signal_date=f.dt
      JOIN market.assets a ON a.asset_id=f.asset_id AND a.asset_type='stock'
        AND NOT (a.metadata ? 'identity_alias_of')
      JOIN LATERAL (
        SELECT v.* FROM market.asset_lifecycle_versions v
        WHERE v.asset_id=f.asset_id AND v.valid_from<=f.dt AND (v.valid_to IS NULL OR v.valid_to>=f.dt)
          AND v.available_at<=f.decision_cutoff
        ORDER BY {lifecycle_order('v')} LIMIT 1
      ) life ON life.lifecycle_status='active' AND life.is_tradable
        AND NOT life.is_st AND NOT life.is_suspended AND life.quality_status<>'conflict'
      LEFT JOIN market.canonical_bars_daily_v2 entry ON entry.asset_id=f.asset_id AND entry.dt=r.entry_date
      LEFT JOIN market.canonical_bars_daily_v2 exit_bar ON exit_bar.asset_id=f.asset_id AND exit_bar.dt=r.exit_date_{horizon}d
      WHERE f.dt BETWEEN %(start)s AND %(end)s
        AND f.input_contract_version='pit-input-v1' AND r.input_contract_version='pit-input-v1'
        AND f.available_at<=f.decision_cutoff
        AND f.decision_cutoff<=((f.dt+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
        AND r.label_available_at_{horizon}d<=statement_timestamp()
        AND (%(label_cutoff)s::date IS NULL OR
             (r.exit_date_{horizon}d < %(label_cutoff)s::date
              AND r.label_available_at_{horizon}d < (%(label_cutoff)s::date::timestamp AT TIME ZONE 'Asia/Shanghai')))
    ), eligible AS (
      SELECT * FROM raw WHERE forward_return IS NOT NULL AND executable
    ), ranked AS (
      SELECT *, percent_rank() OVER (PARTITION BY dt ORDER BY factor_value) AS factor_rank,
                percent_rank() OVER (PARTITION BY dt ORDER BY forward_return) AS return_rank
      FROM eligible WHERE factor_value IS NOT NULL
    ), membership AS (
      SELECT dt,asset_id,(factor_rank >= 0.8)::int AS is_top
      FROM ranked
    ), membership_lagged AS (
      SELECT *,lag(is_top) OVER (PARTITION BY asset_id ORDER BY dt) AS previous_is_top
      FROM membership
    ), turnover_daily AS (
      SELECT dt,
             sum(abs(is_top-previous_is_top))::double precision
               / nullif(2.0*sum(is_top),0) AS turnover
      FROM membership_lagged
      WHERE previous_is_top IS NOT NULL
      GROUP BY dt
    ), daily AS (
      SELECT dt, count(*) AS n, corr(factor_rank, return_rank) AS rank_ic,
             avg(forward_return) FILTER (WHERE factor_rank >= 0.8)
             - avg(forward_return) FILTER (WHERE factor_rank <= 0.2) AS spread
      FROM ranked GROUP BY dt HAVING count(*) >= 30
    )
    SELECT (SELECT count(*) FROM ranked) AS observation_count,
           count(rank_ic) AS date_count,
           array_agg(rank_ic ORDER BY dt) AS daily_ics,
           (SELECT max(outcome_end) FROM ranked) AS outcome_end,
           (SELECT bool_and(strict_pit_ready) FROM ranked) AS strict_pit_ready,
           (SELECT count(*) FROM raw WHERE forward_return IS NOT NULL) AS raw_eligible_count,
           (SELECT count(*) FROM eligible) AS eligible_count,
           avg(rank_ic) AS rank_ic_mean,
           stddev_samp(rank_ic) AS rank_ic_stddev,
           avg((rank_ic > 0)::int::double precision) AS sign_consistency,
           avg(spread) AS top_bottom_spread_mean,
           (SELECT avg(turnover) FROM turnover_daily) AS turnover_mean
    FROM daily
    """
    row = cur.execute(
        query,
        {"direction": direction, "start": start, "end": end, "label_cutoff": label_cutoff, "horizon": horizon},
    ).fetchone()
    result = dict(row)
    eligible = result.pop("eligible_count") or 0
    result["coverage"] = (result["observation_count"] or 0) / eligible if eligible else 0.0
    mean = result.get("rank_ic_mean")
    stddev = result.get("rank_ic_stddev")
    result["rank_ic_ir"] = mean / stddev if mean is not None and stddev not in (None, 0) else None
    result["p_value"] = hac_p_value(result.pop("daily_ics") or [], max(0,horizon-1))
    result["validation_version"] = VALIDATION_VERSION
    result["input_contract_version"] = INPUT_CONTRACT_VERSION
    result["unknown_lifecycle_policy"] = "exclude_unknown_missing_conflict_st_suspended; unverified_diagnostic_only"
    result["future_exit_filter"] = "conditional diagnostic subset; not an executable portfolio backtest"
    result["cost_adjusted"] = True
    result["round_trip_bps"] = round_trip_bps
    result["execution_constraints"] = "calendar_positive_volume_nonlocked_daily_bars"
    result["execution_limitations"] = "Daily bars cannot prove opening/closing fill or borrow availability; long-short spread is diagnostic, not executable portfolio return."
    result["execution_coverage"] = eligible / result["raw_eligible_count"] if result["raw_eligible_count"] else 0
    spread = result.get("top_bottom_spread_mean")
    result["net_spread_mean"] = spread - 2 * round_trip_bps / 10000 if spread is not None else None
    result["outcome_end"] = str(result["outcome_end"]) if result["outcome_end"] else None
    return result


def bh_q_values(items: list[tuple[str, float | None]]) -> dict[str, float | None]:
    present = sorted((p, key) for key, p in items if p is not None)
    count = len(present)
    result: dict[str, float | None] = {key: None for key, _ in items}
    running = 1.0
    for rank_from_end, (p_value, key) in enumerate(reversed(present), start=1):
        rank = count - rank_from_end + 1
        running = min(running, p_value * count / rank)
        result[key] = min(1.0, running)
    return result


def passed(metrics: dict[str, Any], q_value: float | None, gates: dict[str, Any]) -> bool:
    return (
        metrics.get("input_contract_version") == INPUT_CONTRACT_VERSION
        and metrics.get("strict_pit_ready") is True
        and metrics.get("cost_adjusted") is True
        and bool(metrics.get("execution_constraints"))
        and (metrics.get("net_spread_mean") or 0) > 0
        and (metrics.get("date_count") or 0) >= int(gates.get("min_test_dates", 40))
        and (metrics.get("coverage") or 0) >= float(gates.get("min_coverage", 0.60))
        and abs(metrics.get("rank_ic_mean") or 0) >= float(gates.get("min_abs_rank_ic", 0.02))
        and (metrics.get("rank_ic_ir") or 0) >= float(gates.get("min_rank_ic_ir", 0.20))
        and q_value is not None
        and q_value <= float(gates.get("max_q_value", 0.10))
        and (metrics.get("sign_consistency") or 0) >= float(gates.get("min_sign_consistency", 0.55))
        and (metrics.get("top_bottom_spread_mean") or 0)
            > float(gates.get("min_top_bottom_spread", 0.0))
        and metrics.get("turnover_mean") is not None
        and metrics["turnover_mean"] <= float(gates.get("max_turnover", 0.50))
    )


def empty_metrics() -> dict[str, Any]:
    return {
        "observation_count": 0,
        "date_count": 0,
        "coverage": 0.0,
        "rank_ic_mean": None,
        "rank_ic_stddev": None,
        "rank_ic_ir": None,
        "sign_consistency": None,
        "top_bottom_spread_mean": None,
        "turnover_mean": None,
        "p_value": None,
    }


def feature_coverage(cur, fields: set[str], start: dt.date, end: dt.date) -> dict[str, float]:
    if not fields:
        return {}
    columns = ", ".join(
        f"count(\"{field}\")::double precision / nullif(count(*),0) AS \"{field}\""
        for field in sorted(fields)
    )
    row = cur.execute(
        f"SELECT {columns} FROM factor.feature_daily WHERE dt BETWEEN %s AND %s",
        (start, end),
    ).fetchone()
    return {field: float(row[field] or 0.0) for field in fields}


def normalize_required_features(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.strip().removeprefix("{").removesuffix("}")
        values = [item.strip().strip('"') for item in value.split(",") if item.strip()]
    else:
        values = [str(item) for item in value]
    invalid = set(values) - FEATURES
    if invalid:
        raise ValueError(f"unsupported required_features: {sorted(invalid)}")
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-start", required=True)
    parser.add_argument("--train-end", required=True)
    parser.add_argument("--test-start", required=True)
    parser.add_argument("--test-end", required=True)
    parser.add_argument("--horizon", type=int, choices=(5, 20, 60), default=20)
    parser.add_argument("--status", default="candidate,shadow", help="Factor lifecycle statuses.")
    parser.add_argument("--promote-qualified", action="store_true")
    parser.add_argument("--round-trip-bps", type=float, default=50.0)
    parser.add_argument("--factor-name", action="append")
    args = parser.parse_args()
    dates = [dt.date.fromisoformat(value) for value in (args.train_start, args.train_end, args.test_start, args.test_end)]
    train_start, train_end, test_start, test_end = dates
    if not (train_start <= train_end < test_start <= test_end):
        parser.error("dates must satisfy train_start <= train_end < test_start <= test_end")
    if args.round_trip_bps < 0:
        parser.error("round-trip-bps must be nonnegative")
    statuses = [x.strip() for x in args.status.split(",") if x.strip()]

    with connect(autocommit=False) as conn:
        with conn.cursor() as cur:
            factors = cur.execute(
                """SELECT factor_id, factor_name, direction, expression, promotion_gates, lifecycle_status,
                          required_features, version
                   FROM factor.definitions WHERE lifecycle_status=ANY(%s) AND (%s::text[] IS NULL OR factor_name=ANY(%s)) ORDER BY factor_name""",
                (statuses, args.factor_name, args.factor_name),
            ).fetchall()
            if not factors:
                raise ValueError("No matching factors; refusing an empty successful experiment")
            cur.execute("SET LOCAL statement_timeout='180s'")
            inputs = cur.execute("""SELECT
                (SELECT max(updated_at) FROM factor.feature_daily WHERE dt BETWEEN %s AND %s) features,
                (SELECT max(updated_at) FROM factor.forward_returns WHERE signal_date BETWEEN %s AND %s) labels""",
                (train_start,test_end,train_start,test_end)).fetchone()
            fingerprint = hashlib.sha256(json.dumps({"version": VALIDATION_VERSION,
                "dates": dates, "horizon": args.horizon, "cost": args.round_trip_bps,
                "factors": factors, "inputs": inputs}, sort_keys=True, default=str).encode()).hexdigest()
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("factor-eval:"+fingerprint,))
            previous = cur.execute("""SELECT run_id FROM factor.experiment_runs
                WHERE status='complete' AND config->>'input_fingerprint'=%s LIMIT 1""", (fingerprint,)).fetchone()
            if previous:
                print(f"unchanged inputs; reused completed run_id={previous['run_id']}")
                return 0
            run = cur.execute(
                """
                INSERT INTO factor.experiment_runs (
                  train_start, train_end, test_start, test_end, horizon_days,
                  universe, config, code_version
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING run_id
                """,
                (train_start, train_end, test_start, test_end, args.horizon,
                 jsonb({"asset_type": "stock"}),
                 jsonb({
                     "statuses": statuses, "input_fingerprint": fingerprint, "validation_version": VALIDATION_VERSION, "round_trip_bps": args.round_trip_bps,
                     "entry_price": "next_session_open",
                     "label_policy": "train_labels_available_before_test_start",
                 }),
                 git_version()),
            ).fetchone()
            run_id = run["run_id"]
            for factor in factors:
                factor["required_features"] = normalize_required_features(factor["required_features"])
            required_fields = {
                field for factor in factors for field in (factor["required_features"] or [])
            }
            coverage_by_field = feature_coverage(cur, required_fields, test_start, test_end)
            evaluations: dict[tuple[str, str], dict[str, Any]] = {}
            total_evaluations = len(factors) * 2
            completed_evaluations = 0
            for factor in factors:
                expression_sql = compile_expression(factor["expression"])
                min_coverage = float(factor["promotion_gates"].get("min_coverage", 0.60))
                deficient = {
                    field: coverage_by_field.get(field, 0.0)
                    for field in (factor["required_features"] or [])
                    if coverage_by_field.get(field, 0.0) < min_coverage
                }
                if deficient:
                    print(
                        f"skipping factor={factor['factor_name']} insufficient_test_coverage={deficient}",
                        flush=True,
                    )
                    evaluations[(str(factor["factor_id"]), "train")] = empty_metrics()
                    evaluations[(str(factor["factor_id"]), "test")] = empty_metrics()
                    completed_evaluations += 2
                    continue
                for sample, start, end, label_cutoff in (
                    ("train", train_start, train_end, test_start),
                    ("test", test_start, test_end, None),
                ):
                    print(
                        f"evaluating {completed_evaluations + 1}/{total_evaluations} "
                        f"factor={factor['factor_name']} sample={sample} range={start}..{end}",
                        flush=True,
                    )
                    evaluations[(str(factor["factor_id"]), sample)] = evaluate(
                        cur, expression_sql, factor["direction"], start, end, args.horizon,
                        label_cutoff, args.round_trip_bps,
                    )
                    completed_evaluations += 1

            q_values = bh_q_values([
                (factor_id, metrics.get("p_value"))
                for (factor_id, sample), metrics in evaluations.items() if sample == "test"
            ])
            for factor in factors:
                factor_id = str(factor["factor_id"])
                for sample in ("train", "test"):
                    metrics = evaluations[(factor_id, sample)]
                    q_value = q_values.get(factor_id) if sample == "test" else None
                    gate_pass = sample == "test" and passed(metrics, q_value, factor["promotion_gates"])
                    cur.execute(
                        """
                        INSERT INTO factor.evaluations (
                          run_id, factor_id, sample_name, observation_count, date_count,
                          coverage, rank_ic_mean, rank_ic_stddev, rank_ic_ir,
                          sign_consistency, top_bottom_spread_mean, turnover_mean,
                          p_value, q_value, passed_gates, metrics
                        ) VALUES (%(run_id)s,%(factor_id)s,%(sample)s,%(observation_count)s,%(date_count)s,
                          %(coverage)s,%(rank_ic_mean)s,%(rank_ic_stddev)s,%(rank_ic_ir)s,
                          %(sign_consistency)s,%(top_bottom_spread_mean)s,%(turnover_mean)s,
                          %(p_value)s,%(q_value)s,%(passed_gates)s,%(metrics)s)
                        """,
                        {"run_id": run_id, "factor_id": factor["factor_id"], "sample": sample,
                         **metrics, "q_value": q_value, "passed_gates": gate_pass,
                         "metrics": jsonb({**metrics, "horizon_days": args.horizon, "factor_version": factor["version"]})},
                    )
                    if args.promote_qualified and gate_pass and factor["lifecycle_status"] == "candidate":
                        cur.execute(
                            "UPDATE factor.definitions SET lifecycle_status='shadow', updated_at=now() WHERE factor_id=%s",
                            (factor["factor_id"],),
                        )
                        cur.execute(
                            """INSERT INTO factor.lifecycle_events
                               (factor_id,from_status,to_status,run_id,reason)
                               VALUES (%s,'candidate','shadow',%s,'Passed configured out-of-sample gates')""",
                            (factor["factor_id"], run_id),
                        )
            cur.execute(
                "UPDATE factor.experiment_runs SET status='complete', finished_at=clock_timestamp() WHERE run_id=%s",
                (run_id,),
            )
        conn.commit()
    print(f"run_id={run_id} factors={len(factors)} horizon={args.horizon}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

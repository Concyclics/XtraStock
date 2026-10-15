#!/usr/bin/env python3
"""Generate deterministic interaction candidates using the safe factor DSL."""
from __future__ import annotations

import argparse
import itertools
import hashlib
import json

from common import connect, jsonb
from factor_dsl import FEATURES, compile_expression
from factor_input_contract import eligible_mining_fields


DEFAULT_FIELDS = [
    "momentum_20d",
    "low_volatility_20d",
    "money_flow_intensity_5d",
    "research_breadth_20d",
    "rating_upgrade_20d",
    "event_sentiment_5d",
    "graph_event_impulse_30d",
    "graph_novelty_30d",
    "graph_rating_momentum_60d",
    "graph_forecast_eps_revision_90d",
    "graph_demand_impulse_30d",
    "graph_two_hop_exposure",
    "graph_evidence_diversity",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-candidates", type=int, default=24)
    parser.add_argument("--fields", help="Comma-separated whitelist subset.")
    parser.add_argument("--min-input-coverage",type=float,default=.60)
    parser.add_argument("--dry-run",action="store_true")
    args = parser.parse_args()
    if not 0 < args.min_input_coverage <= 1:
        parser.error("min-input-coverage must be in (0,1]")
    fields = [x.strip() for x in args.fields.split(",")] if args.fields else DEFAULT_FIELDS
    invalid = set(fields) - FEATURES
    if invalid:
        parser.error(f"unsupported fields: {sorted(invalid)}")

    with connect() as conn:
        fields_sql=",".join(f'count("{f}") AS "{f}_n",stddev_samp("{f}") AS "{f}_sd"' for f in fields)
        row=conn.execute(f"""SELECT count(*) AS total,{fields_sql}
          FROM factor.feature_daily WHERE dt=(SELECT max(dt) FROM factor.feature_daily)
            AND input_contract_version='pit-input-v1' AND available_at<=decision_cutoff
            AND input_quality->>'strict_pit_ready'='true'""").fetchone()
        stats={f:{'observed':row[f+'_n'],'stddev':row[f+'_sd'],'total':row['total']} for f in fields}
        fields,skipped=eligible_mining_fields(stats,args.min_input_coverage)
        print(json.dumps({'eligible_fields':fields,'skipped_fields':skipped,'input_rows':row['total']},ensure_ascii=False),flush=True)
        existing = conn.execute("SELECT expression FROM factor.definitions").fetchall()
    seen = {json.dumps(row["expression"], sort_keys=True, separators=(",", ":")) for row in existing}
    candidates = []
    for left, right in itertools.combinations(fields, 2):
        for op in ("add", "subtract"):
            expression = {
                "op": op,
                "left": {"op": "zscore", "arg": {"op": "field", "field": left}},
                "right": {"op": "zscore", "arg": {"op": "field", "field": right}},
            }
            compile_expression(expression)
            canonical = json.dumps(expression, sort_keys=True, separators=(",", ":"))
            if canonical in seen or args.max_candidates <= 0:
                continue
            seen.add(canonical)
            candidates.append((f"auto_{left}_{op}_{right}", left, right, op, expression))
            if len(candidates) >= max(0, args.max_candidates):
                break
        if len(candidates) >= max(0, args.max_candidates):
            break

    if args.dry_run or not candidates:
        print(json.dumps({'dry_run':args.dry_run,'generated':len(candidates),'inserted':0,
            'candidate_names':[c[0] for c in candidates]},ensure_ascii=False))
        return 0
    with connect(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO factor.definitions (
                  factor_name, display_name, family, description, direction,
                  expression, required_features, lifecycle_status, owner, metadata
                ) VALUES (
                  %(factor_name)s, %(display_name)s, 'interaction', %(description)s,
                  1, %(expression)s, %(required_features)s, 'candidate',
                  'auto_miner:v1', %(metadata)s
                )
                ON CONFLICT (factor_name) DO NOTHING
                """,
                [
                    {
                        "factor_name": name,
                        "display_name": f"自动候选: {left} {op} {right}",
                        "description": "由白名单基础特征生成的可审计双因子候选。",
                        "expression": jsonb(expression),
                        "required_features": [left, right],
                        "metadata": jsonb({"generator": "pairwise:v2", "operation": op, "expression_sha256": hashlib.sha256(json.dumps(expression, sort_keys=True).encode()).hexdigest()}),
                    }
                    for name, left, right, op, expression in candidates
                ],
            )
            inserted = cur.rowcount
    print(f"generated={len(candidates)} inserted={inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

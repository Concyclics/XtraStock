#!/usr/bin/env python3
"""Build a causal total-return factor from versioned corporate actions."""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from common import connect

PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from v3_ops import record_quality, set_dataset_watermark, stage_run  # noqa: E402


TZ = ZoneInfo("Asia/Shanghai")

BUILD_SQL = """
CREATE TEMP TABLE adjustment_factors_v3_selected ON COMMIT DROP AS
WITH prior AS (
  SELECT DISTINCT ON (asset_id)
         asset_id,split_factor AS prior_split_factor,
         total_return_factor AS prior_total_return_factor,
         quality_status AS prior_quality_status,
         available_at AS prior_available_at
  FROM market.adjustment_factors_daily
  WHERE dt < %(since)s
  ORDER BY asset_id,dt DESC
), source_revisions AS (
  SELECT ca.*,row_number() OVER (
    PARTITION BY source_name,source_external_id
    ORDER BY revision_no DESC,available_at DESC,action_id DESC
  ) AS document_revision
  FROM market.corporate_actions ca
), ranked_actions AS (
  SELECT ca.*,
         row_number() OVER (
           PARTITION BY asset_id,ex_date,
             CASE WHEN action_type IN ('cash_dividend','stock_dividend','split','reverse_split')
                  THEN 'distribution' ELSE action_type END
           ORDER BY announced_at DESC,available_at DESC,revision_no DESC,source_external_id DESC,action_id DESC
         ) AS source_rank
  FROM source_revisions ca
  WHERE document_revision=1 AND ex_date BETWEEN %(since)s AND %(until)s
    AND quality_status <> 'rejected'
), action_day AS (
  SELECT
    asset_id,ex_date,
    sum(coalesce(stock_dividend_ratio,0) + coalesce(split_ratio,0)) AS extra_shares,
    sum(coalesce(cash_dividend,0)) AS cash_dividend,
    bool_or(action_type='rights_issue') AS has_rights,
    bool_or(quality_status <> 'verified') AS has_unverified,
    max(available_at) AS action_available_at,
    jsonb_agg(jsonb_build_object(
      'action_id',action_id,'action_type',action_type,'quality_status',quality_status,
      'source_name',source_name,'source_document_id',source_document_id
    ) ORDER BY action_type,action_id) AS actions
  FROM ranked_actions
  WHERE source_rank=1
  GROUP BY asset_id,ex_date
), increments AS (
  SELECT
    b.asset_id,b.dt,b.close,b.available_at AS bar_available_at,b.quality_status AS bar_quality,
    coalesce(p.prior_split_factor,1.0) AS prior_split_factor,
    coalesce(p.prior_total_return_factor,1.0) AS prior_total_return_factor,
    p.prior_quality_status,p.prior_available_at,
    coalesce(a.extra_shares,0) AS extra_shares,
    coalesce(a.cash_dividend,0) AS cash_dividend,
    coalesce(a.has_rights,false) AS has_rights,
    coalesce(a.has_unverified,false) AS has_unverified,
    a.action_available_at,coalesce(a.actions,'[]'::jsonb) AS actions,
    CASE
      WHEN coalesce(a.has_rights,false) THEN 1.0
      ELSE greatest(1e-12,1.0 + coalesce(a.extra_shares,0) + coalesce(a.cash_dividend,0)/nullif(b.close,0))
    END AS daily_increment
  FROM market.canonical_bars_daily_v2 b
  LEFT JOIN action_day a ON a.asset_id=b.asset_id AND a.ex_date=b.dt
  LEFT JOIN prior p ON p.asset_id=b.asset_id
  WHERE b.dt BETWEEN %(since)s AND %(until)s
), cumulative AS (
  SELECT *,
         prior_total_return_factor * exp(sum(ln(daily_increment)) OVER (
           PARTITION BY asset_id ORDER BY dt ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
         )) AS total_return_factor,
         greatest(
           prior_available_at,
           max(action_available_at) OVER (
           PARTITION BY asset_id ORDER BY dt ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
           )
         ) AS latest_action_available_at,
         bool_or(has_rights) OVER (
           PARTITION BY asset_id ORDER BY dt ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
         ) AS cumulative_rights_gap,
         bool_or(has_unverified) OVER (
           PARTITION BY asset_id ORDER BY dt ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
         ) AS cumulative_unverified
  FROM increments
)
SELECT
  asset_id,dt,
  prior_split_factor * exp(sum(ln(greatest(1e-12,1.0+extra_shares))) OVER (
    PARTITION BY asset_id ORDER BY dt ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
  )) AS split_factor,
  total_return_factor,
  greatest(bar_available_at,coalesce(latest_action_available_at,bar_available_at)) AS available_at,
  CASE
    WHEN NOT %(source_complete)s THEN 'incomplete'
    WHEN prior_quality_status='incomplete' OR cumulative_rights_gap THEN 'incomplete'
    WHEN prior_quality_status='conflict' THEN 'conflict'
    WHEN prior_quality_status='unverified' OR cumulative_unverified THEN 'unverified'
    WHEN bar_quality IN ('conflict','incomplete','rejected') THEN 'conflict'
    ELSE 'verified'
  END AS quality_status,
  jsonb_build_object(
    'method','causal_forward_total_return:v1',
    'prior_total_return_factor',prior_total_return_factor,
    'prior_split_factor',prior_split_factor,
    'daily_increment',daily_increment,
    'actions',actions,
    'rights_issue_not_modeled',cumulative_rights_gap
  ) AS provenance
FROM cumulative
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Build V3 corporate-action adjustment factors.")
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--allow-quality-fail", action="store_true")
    args = parser.parse_args()
    since = dt.date.fromisoformat(args.since)
    until = dt.date.fromisoformat(args.until)
    if since > until:
        parser.error("--since must not be after --until")

    historical_span = (until - since).days >= 365
    with connect(autocommit=True, admin=False) as conn:
        source_watermark = conn.execute(
            """
            SELECT coverage_pct,quality_status,completed_at
            FROM ops.dataset_watermarks
            WHERE dataset_key='market.corporate_actions' AND partition_key='history'
            """
        ).fetchone()
    source_coverage = float(source_watermark["coverage_pct"] or 0) if source_watermark else 0.0
    source_quality = str(source_watermark["quality_status"] or "unknown") if source_watermark else "missing"
    source_complete = not historical_span or (
        source_coverage >= 99.0 and source_quality not in {"fail", "degraded", "unknown"}
    )

    with stage_run(
        "finance_v3_evening", "adjustment_factors_v3", expected_date=until,
        metadata={"since": since.isoformat(), "until": until.isoformat()},
    ) as run:
        with connect(autocommit=False, admin=False) as conn:
            with conn.cursor() as cur:
                cur.execute(BUILD_SQL, {
                    "since": since, "until": until, "source_complete": source_complete,
                })
                cur.execute(
                    "DELETE FROM market.adjustment_factors_daily WHERE dt BETWEEN %s AND %s",
                    (since, until),
                )
                cur.execute(
                    """
                    INSERT INTO market.adjustment_factors_daily (
                      asset_id,dt,split_factor,total_return_factor,source_version,
                      available_at,quality_status,provenance,updated_at
                    )
                    SELECT asset_id,dt,split_factor,total_return_factor,
                           'corporate_actions:causal_v2',available_at,quality_status,provenance,now()
                    FROM adjustment_factors_v3_selected
                    """
                )
                cur.execute(
                    """
                    SELECT count(*) AS rows,count(DISTINCT asset_id) AS assets,max(dt) AS max_dt,
                           count(*) FILTER (WHERE quality_status='incomplete') AS incomplete,
                           count(*) FILTER (WHERE quality_status='conflict') AS conflicts,
                           count(*) FILTER (WHERE quality_status='unverified') AS unverified
                    FROM adjustment_factors_v3_selected
                    """
                )
                summary = cur.fetchone()
                cur.execute(
                    """
                    SELECT count(*) AS actions,count(DISTINCT asset_id) AS action_assets
                    FROM market.corporate_actions
                    WHERE ex_date <= %s AND quality_status <> 'rejected'
                    """,
                    (until,),
                )
                action_summary = cur.fetchone()
                cur.execute(
                    """
                    WITH ranked AS (
                      SELECT asset_id,ex_date,action_type,cash_dividend,
                             stock_dividend_ratio,split_ratio,rights_ratio,rights_price,
                             CASE WHEN action_type IN (
                               'cash_dividend','stock_dividend','split','reverse_split'
                             ) THEN 'distribution' ELSE action_type END AS action_family,
                             row_number() OVER (
                               PARTITION BY asset_id,ex_date,
                                 CASE WHEN action_type IN (
                                   'cash_dividend','stock_dividend','split','reverse_split'
                                 ) THEN 'distribution' ELSE action_type END
                               ORDER BY available_at DESC,revision_no DESC,
                                        source_external_id DESC,action_id DESC
                             ) AS rn,
                             count(*) OVER (
                               PARTITION BY asset_id,ex_date,
                                 CASE WHEN action_type IN (
                                   'cash_dividend','stock_dividend','split','reverse_split'
                                 ) THEN 'distribution' ELSE action_type END
                             ) AS versions
                      FROM market.corporate_actions
                      WHERE ex_date BETWEEN %s AND %s AND quality_status <> 'rejected'
                    ), grouped AS (
                      SELECT asset_id,ex_date,max(versions) AS versions,
                             count(DISTINCT ROW(
                               action_type,cash_dividend,stock_dividend_ratio,
                               split_ratio,rights_ratio,rights_price
                             )) AS distinct_terms
                      FROM ranked
                      GROUP BY asset_id,ex_date,action_family
                    )
                    SELECT coalesce(sum(versions-1),0) AS superseded_versions,
                           count(*) FILTER (WHERE distinct_terms>1) AS conflicting_groups
                    FROM grouped
                    """,
                    (since, until),
                )
                duplicate_summary = cur.fetchone()
            conn.commit()
        rows = int(summary["rows"] or 0)
        assets = int(summary["assets"] or 0)
        incomplete = int(summary["incomplete"] or 0)
        conflicts = int(summary["conflicts"] or 0)
        unverified = int(summary["unverified"] or 0)
        actions = int(action_summary["actions"] or 0)
        action_assets = int(action_summary["action_assets"] or 0)
        superseded_versions = int(duplicate_summary["superseded_versions"] or 0)
        conflicting_groups = int(duplicate_summary["conflicting_groups"] or 0)
        historical_source_missing = historical_span and assets >= 100 and actions == 0
        historical_source_incomplete = historical_span and not source_complete
        quality_fail = (
            rows == 0 or conflicts > 0 or historical_source_missing
            or historical_source_incomplete
        )
        quality = "fail" if quality_fail else ("warn" if incomplete or unverified else "pass")
        record_quality(
            run["run_id"], "market.adjustment_factors_daily", "rights_issue_model_gaps",
            status="warn" if incomplete else "pass", severity="warning",
            observed_value=float(incomplete), threshold_value=0, affected_rows=incomplete,
        )
        record_quality(
            run["run_id"], "market.adjustment_factors_daily",
            "duplicate_distribution_version_selection",
            status="warn" if conflicting_groups else "pass",
            severity="warning" if conflicting_groups else "info",
            observed_value=float(conflicting_groups), threshold_value=0,
            affected_rows=superseded_versions,
            details={
                "selection_rule": "latest_available_at_per_asset_ex_date_action_family",
                "superseded_versions": superseded_versions,
            },
        )
        record_quality(
            run["run_id"], "market.adjustment_factors_daily", "corporate_action_parse_coverage",
            status="fail" if historical_source_incomplete else "pass", severity="error",
            observed_value=source_coverage, threshold_value=99.0,
            affected_rows=rows if historical_source_incomplete else 0,
            details={
                "partition_key": "history", "source_quality_status": source_quality,
                "historical_span_days": (until - since).days + 1,
            },
        )
        record_quality(
            run["run_id"], "market.adjustment_factors_daily", "source_conflicts",
            status="fail" if conflicts else "pass", severity="error",
            observed_value=float(conflicts), threshold_value=0, affected_rows=conflicts,
        )
        record_quality(
            run["run_id"], "market.adjustment_factors_daily", "corporate_action_source_coverage",
            status="fail" if historical_source_missing else "pass", severity="error",
            observed_value=float(action_assets), threshold_value=1 if historical_source_missing else 0,
            affected_rows=rows if historical_source_missing else 0,
            details={
                "actions": actions,
                "action_assets": action_assets,
                "historical_span_days": (until - since).days + 1,
                "identity_only_is_not_verified": True,
            },
        )
        data_through = dt.datetime.combine(summary["max_dt"] or until, dt.time(15, 5), TZ)
        set_dataset_watermark(
            "market.adjustment_factors_daily", schema_version="v3", run_id=run["run_id"],
            expected_date=until, data_through=data_through,row_count=rows,
            eligible_count=rows,coverage_pct=100.0 if rows else 0.0,
            quality_status=quality,source_version="corporate_actions:causal_v2",
            details={
                "assets": assets, "incomplete": incomplete,
                "conflicts": conflicts, "unverified": unverified,
                "actions": actions, "action_assets": action_assets,
                "historical_source_missing": historical_source_missing,
                "historical_source_incomplete": historical_source_incomplete,
                "corporate_action_coverage_pct": source_coverage,
                "corporate_action_quality_status": source_quality,
                "superseded_action_versions": superseded_versions,
                "conflicting_action_groups": conflicting_groups,
            },
        )
        run["quality_status"] = quality
        run["output_watermarks"] = {"market.adjustment_factors_daily": str(summary["max_dt"])}
        if quality_fail and not args.allow_quality_fail:
            raise RuntimeError(
                "adjustment factor hard gate failed "
                f"rows={rows} conflicts={conflicts} actions={actions}"
            )
    print({
        **dict(summary), "actions": actions, "action_assets": action_assets,
        "superseded_action_versions": superseded_versions,
        "conflicting_action_groups": conflicting_groups,
        "quality_status": quality,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

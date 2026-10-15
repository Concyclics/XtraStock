#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."

backfill_status=0
.venv/bin/python -u scripts/pipeline/market_kline_backfill.py \
  --beg 20260702 --end 20260717 --adjust qfq \
  --asset-type stock --asset-type index --workers 8 --retries 2 \
  || backfill_status=$?

# Materialize enough history for 60/90-day rolling features. Missing preferred
# rows remain explicit Sina fallbacks instead of silently disappearing.
.venv/bin/python -u scripts/analytics/sync_canonical_daily_bars.py \
  --since 2020-01-01 --until 2026-07-17 --asset-types stock,index

.venv/bin/python -u scripts/analytics/build_technical_signals.py \
  --since 2026-07-02 --until 2026-07-17 --asset-types stock,index
.venv/bin/python -u scripts/analytics/build_factor_features.py \
  --since 2026-05-18 --until 2026-07-17 --chunk-days 31 --labels-only
.venv/bin/python -u scripts/analytics/build_factor_features.py \
  --since 2026-07-02 --until 2026-07-17 --chunk-days 16 --skip-labels
.venv/bin/python -u scripts/analytics/build_factor_scores.py \
  --since 2026-07-02 --until 2026-07-17

exit "$backfill_status"

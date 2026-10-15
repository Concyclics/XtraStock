#!/usr/bin/env bash
set -euo pipefail
XTRASTOCK_PYTHON="${XTRASTOCK_PYTHON:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)/.venv/bin/python}"
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

echo "[postprocess] start $(date -Is)"

"$XTRASTOCK_PYTHON" -u scripts/pipeline/market_kline_backfill.py \
  --beg 20200101 \
  --end 20500101 \
  --asset-type stock \
  --asset-type index \
  --workers 2 \
  --retries 1 \
  --skip-ok \
  --skip-failed

echo "[postprocess] analyze bars_daily $(date -Is)"
"$XTRASTOCK_PYTHON" -m xtrastock.psql --role admin -c 'ANALYZE market.bars_daily;'

echo "[postprocess] build technical $(date -Is)"
"$XTRASTOCK_PYTHON" -u scripts/analytics/build_technical_signals.py \
  --since 2020-01-01 \
  --until 2026-07-02

echo "[postprocess] rebuild event returns $(date -Is)"
"$XTRASTOCK_PYTHON" -u scripts/analytics/build_financial_statement_metrics.py \
  --period 2026Q1 \
  --latest-date 2026-07-02 \
  --skip-metrics

echo "[postprocess] done $(date -Is)"

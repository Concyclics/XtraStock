#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
XTRASTOCK_PYTHON="${XTRASTOCK_PYTHON:-$ROOT_DIR/../../.venv/bin/python}"
# Daily/manual default shares the transactional incremental importer.
# The previous full TSV loader remains available only as an explicit rebuild.
if [[ "${1:-}" != "--full-rebuild" ]]; then
  exec "$XTRASTOCK_PYTHON" "$ROOT_DIR/scripts/pipeline/incremental_import.py" --source eastmoney "$@"
fi
shift
PSQL=("$XTRASTOCK_PYTHON" -m xtrastock.psql --role admin)
TSV_FILE="${TSV_FILE:-$ROOT_DIR/build/eastmoney_reports_import/eastmoney_reports.tsv}"


if [[ ! -s "$TSV_FILE" ]]; then
  echo "missing or empty TSV: $TSV_FILE" >&2
  exit 1
fi

echo "[reports-import] setup staging"
"${PSQL[@]}" < "$ROOT_DIR/docker/postgres/import_eastmoney_reports_setup.sql"

echo "[reports-import] copy TSV"
"${PSQL[@]}" -c "COPY staging.eastmoney_reports (
  source_type, source_name, external_id, report_type, title, publisher, published_at,
  url, language, content_text, content_md, raw_text, content_hash, report_date,
  institution, rating, rating_change, industry_name, stock_code, stock_name,
  source_url, web_text_status, pdf_status, ocr_status, pdf_path, pdf_size_bytes,
  ocr_md_path, ocr_md_size_bytes, ocr_quality_json_path, ocr_quality_md_path,
  ocr_quality, forecast_current_year, forecast_current_eps, forecast_current_pe,
  forecast_next_year, forecast_next_eps, forecast_next_pe, forecast_later_year,
  forecast_later_eps, forecast_later_pe, metadata
) FROM STDIN WITH (FORMAT csv, DELIMITER E'\t', HEADER true, QUOTE '\"', ESCAPE '\"')" < "$TSV_FILE"

echo "[reports-import] materialize"
"${PSQL[@]}" < "$ROOT_DIR/docker/postgres/import_eastmoney_reports_materialize.sql"

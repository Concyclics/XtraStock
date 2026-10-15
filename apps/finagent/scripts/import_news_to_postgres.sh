#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
XTRASTOCK_PYTHON="${XTRASTOCK_PYTHON:-$ROOT_DIR/../../.venv/bin/python}"
# Daily/manual default shares the transactional incremental importer.
# The previous full TSV loader remains available only as an explicit rebuild.
if [[ "${1:-}" != "--full-rebuild" ]]; then
  exec "$XTRASTOCK_PYTHON" "$ROOT_DIR/scripts/pipeline/incremental_import.py" --source news "$@"
fi
shift
PSQL=("$XTRASTOCK_PYTHON" -m xtrastock.psql --role admin)


copy_tsv() {
  local table="$1"
  local columns="$2"
  local file="$3"
  if [[ ! -s "$file" ]]; then
    echo "missing or empty TSV: $file" >&2
    return 1
  fi
  "${PSQL[@]}" -c "COPY ${table} (${columns}) FROM STDIN WITH (FORMAT csv, DELIMITER E'\t', HEADER true, QUOTE '\"', ESCAPE '\"')" < "$file"
}

echo "[news-import] ensure news page columns"
"${PSQL[@]}" < "$ROOT_DIR/docker/postgres/alter_news_articles_page_fields.sql"

echo "[news-import] import People's Daily staging"
"${PSQL[@]}" < "$ROOT_DIR/docker/postgres/import_peoples_daily_setup.sql"
copy_tsv \
  "staging.peoples_daily_articles" \
  "external_id, source_type, source_name, title, author, publisher, published_at, url, language, content_text, raw_text, content_hash, newspaper_date, page_no, article_no, page_name, page_url, page_pdf_url, page_image_url, section_name, original_file_path, size_bytes, metadata" \
  "$ROOT_DIR/build/peoples_daily_import/peoples_daily_articles.tsv"
"${PSQL[@]}" < "$ROOT_DIR/docker/postgres/import_peoples_daily_materialize.sql"

echo "[news-import] import Xinhua Daily staging"
"${PSQL[@]}" < "$ROOT_DIR/docker/postgres/import_xinhua_daily_setup.sql"
copy_tsv \
  "staging.xinhua_daily_articles" \
  "external_id, source_type, source_name, title, subtitle, author, publisher, published_at, url, language, content_text, raw_text, content_hash, newspaper_date, page_no, article_no, page_name, page_url, page_pdf_url, page_image_url, section_name, original_file_path, raw_html_file_path, size_bytes, raw_html_size_bytes, metadata" \
  "$ROOT_DIR/build/xinhua_daily_import/xinhua_daily_articles.tsv"
"${PSQL[@]}" < "$ROOT_DIR/docker/postgres/import_xinhua_daily_materialize.sql"

echo "[news-import] verify counts"
"${PSQL[@]}" -c "
SELECT d.source_type, d.source_name, count(*) AS documents, min(d.published_at)::date AS min_date, max(d.published_at)::date AS max_date
FROM corpus.documents d
WHERE (d.source_type, d.source_name) IN (('peoples_daily', 'peoples_daily'), ('news', 'xinhua_daily'))
GROUP BY d.source_type, d.source_name
ORDER BY d.source_name;

SELECT source_name, count(*) AS articles, count(page_name) AS with_page_name, min(newspaper_date) AS min_date, max(newspaper_date) AS max_date
FROM news.articles
WHERE source_name IN ('peoples_daily', 'xinhua_daily')
GROUP BY source_name
ORDER BY source_name;
"

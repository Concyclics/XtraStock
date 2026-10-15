#!/usr/bin/env bash
set -euo pipefail
# Credentials stay in the environment. psql safely quotes identifiers and literals.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv app_user POSTGRES_APP_USER
\getenv app_password APP_DB_PASSWORD
\getenv reader_user AI_READONLY_DB_USER
\getenv reader_password AI_READONLY_DB_PASSWORD
\getenv database POSTGRES_DB
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user') \gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'reader_user', :'reader_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'reader_user') \gexec
SELECT format('ALTER ROLE %I SET default_transaction_read_only = on', :'reader_user') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I, %I', :'database', :'app_user', :'reader_user') \gexec
SELECT format('CREATE SCHEMA IF NOT EXISTS %I', name)
FROM unnest(ARRAY['corpus','market','analytics','research','company','macro','news','ops','agent','ai','kg','factor','quant']) name \gexec
SELECT format('GRANT USAGE ON SCHEMA %I TO %I, %I', nspname, :'app_user', :'reader_user')
FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname != 'information_schema' \gexec
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I', nspname, :'app_user')
FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname != 'information_schema' \gexec
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON TABLES TO %I', nspname, :'reader_user')
FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname != 'information_schema' \gexec
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO %I', nspname, :'app_user')
FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname != 'information_schema' \gexec
SQL

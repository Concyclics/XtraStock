#!/usr/bin/env bash
set -euo pipefail
FINAGENT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
exec "$FINAGENT_ROOT/.venv/bin/python" "$FINAGENT_ROOT/apps/finagent/scripts/ops/deploy_finagent.py" "$@"

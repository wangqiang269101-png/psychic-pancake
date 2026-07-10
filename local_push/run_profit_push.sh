#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
DEPLOY_ROOT="$(cd "$ROOT/.." && pwd)"
PYTHON="/usr/bin/python3"
FETCH_SCRIPT="$DEPLOY_ROOT/cloud_push/fetch_profit_data.py"
DATA_FILE="$ROOT/data/latest-profit-payload.json"

cd "$ROOT"
"$PYTHON" "$FETCH_SCRIPT" "$DATA_FILE"
exec "$PYTHON" "$ROOT/profit_push.py" "$@"

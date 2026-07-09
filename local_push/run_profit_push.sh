#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="/Users/wangyongqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"

cd "$ROOT"
exec "$PYTHON" "$ROOT/profit_push.py" "$@"

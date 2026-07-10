#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
# Force arm64 Python so Playwright resolves chrome-*-mac-arm64 (not x64 under Rosetta/Cursor).
if command -v arch >/dev/null 2>&1; then
  PYTHON=(arch -arm64 /usr/bin/python3)
else
  PYTHON=(/usr/bin/python3)
fi
REPO_CLOUD_PUSH="/Users/wangyongqiang/Desktop/Ai 助手/profit-cloud-deploy/cloud_push"
CONFIG="$ROOT/config.json"

# Always prefer the real user cache; ignore Cursor sandbox PLAYWRIGHT_BROWSERS_PATH.
export PLAYWRIGHT_BROWSERS_PATH="$HOME/Library/Caches/ms-playwright"
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
export TZ="${TZ:-Asia/Shanghai}"

if [[ -f "$ROOT/cloud_push/profit_cloud_push.py" ]]; then
  CLOUD_DIR="$ROOT/cloud_push"
elif [[ -f "$REPO_CLOUD_PUSH/profit_cloud_push.py" ]]; then
  CLOUD_DIR="$REPO_CLOUD_PUSH"
else
  echo "找不到 profit_cloud_push.py（已检查 $ROOT/cloud_push 与仓库 cloud_push）" >&2
  exit 1
fi

if [[ -f "$CONFIG" ]]; then
  WEBHOOK="$("${PYTHON[@]}" - "$CONFIG" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print((data.get("wecom_webhook") or "").strip())
PY
)"
  if [[ -n "$WEBHOOK" ]]; then
    export WECOM_WEBHOOK="$WEBHOOK"
  fi
fi

if [[ -z "${WECOM_WEBHOOK:-}" ]]; then
  echo "缺少 WECOM_WEBHOOK（config.json 的 wecom_webhook 或环境变量）" >&2
  exit 1
fi

cd "$CLOUD_DIR"
exec "${PYTHON[@]}" profit_cloud_push.py --source auto "$@"

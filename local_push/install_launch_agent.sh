#!/bin/zsh
set -euo pipefail

SRC_ROOT="/Users/wangyongqiang/Desktop/Ai 助手/profit-cloud-deploy/local_push"
ROOT="$HOME/Library/Application Support/profit-push"
LABEL="com.wangyongqiang.daily-profit-push"
PLIST_SRC="$SRC_ROOT/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs" "$ROOT/outputs" "$ROOT/data"

rsync -a \
  --exclude "logs" \
  --exclude "outputs" \
  --exclude "state.json" \
  --exclude ".profit_push.lock" \
  --exclude "config.json" \
  "$SRC_ROOT/" "$ROOT/"

if [[ ! -f "$ROOT/config.json" ]]; then
  cp "$SRC_ROOT/config.json" "$ROOT/config.json"
fi

chmod +x "$ROOT/run_profit_push.sh" "$ROOT/profit_push.py" "$ROOT/set_webhook.sh"
cp "$PLIST_SRC" "$PLIST_DST"

launchctl bootout "gui/$(id -u)" "$PLIST_DST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL" || true

echo "Installed and started $LABEL"
echo "Logs:"
echo "  $ROOT/logs/launchd.out.log"
echo "  $ROOT/logs/launchd.err.log"
echo "  $ROOT/logs/profit_push.log"

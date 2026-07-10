#!/bin/zsh
set -euo pipefail

DEPLOY_ROOT="/Users/wangyongqiang/Desktop/Ai 助手/profit-cloud-deploy"
SRC_ROOT="$DEPLOY_ROOT/local_push"
CLOUD_SRC="$DEPLOY_ROOT/cloud_push"
ROOT="$HOME/Library/Application Support/profit-push"
LABEL="com.wangyongqiang.daily-profit-push"
PLIST_SRC="$SRC_ROOT/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs" "$ROOT/outputs" "$ROOT/data" "$ROOT/cloud_push"

# Prefer cp over rsync: TCC sometimes blocks rsync chmod on Application Support.
mkdir -p "$ROOT" "$ROOT/cloud_push"
rsync -a --no-perms --no-owner --no-group \
  --exclude "logs" \
  --exclude "outputs" \
  --exclude "state.json" \
  --exclude ".profit_push.lock" \
  --exclude "config.json" \
  "$SRC_ROOT/" "$ROOT/" || true
# Ensure critical scripts are always refreshed even if rsync partially fails.
cp -f "$SRC_ROOT/run_profit_push.sh" "$ROOT/run_profit_push.sh"
cp -f "$SRC_ROOT/profit_push.py" "$ROOT/profit_push.py"
cp -f "$SRC_ROOT/install_launch_agent.sh" "$ROOT/install_launch_agent.sh"
cp -f "$SRC_ROOT/$LABEL.plist" "$ROOT/$LABEL.plist"
[[ -f "$SRC_ROOT/set_webhook.sh" ]] && cp -f "$SRC_ROOT/set_webhook.sh" "$ROOT/set_webhook.sh" || true

# Bundle scraper so launchd can fetch latest Power BI data without relying on cwd layout.
rsync -a --no-perms --no-owner --no-group \
  --exclude "logs" \
  --exclude "outputs" \
  --exclude ".state" \
  --exclude "__pycache__" \
  --exclude ".env" \
  "$CLOUD_SRC/" "$ROOT/cloud_push/" || true
cp -f "$CLOUD_SRC/profit_cloud_push.py" "$ROOT/cloud_push/profit_cloud_push.py"
cp -f "$CLOUD_SRC/powerbi_scraper.py" "$ROOT/cloud_push/powerbi_scraper.py"
cp -f "$CLOUD_SRC/fetch_profit_data.py" "$ROOT/cloud_push/fetch_profit_data.py"
[[ -f "$CLOUD_SRC/requirements.txt" ]] && cp -f "$CLOUD_SRC/requirements.txt" "$ROOT/cloud_push/requirements.txt" || true
if [[ ! -f "$ROOT/config.json" ]]; then
  cp "$SRC_ROOT/config.json" "$ROOT/config.json"
fi

# Keep lag policy / webhook aligned with repo config (T-2 cadence).
python3 - <<'PY'
import json
from pathlib import Path
root = Path.home() / "Library/Application Support/profit-push/config.json"
src = Path("/Users/wangyongqiang/Desktop/Ai 助手/profit-cloud-deploy/local_push/config.json")
cfg = json.loads(root.read_text(encoding="utf-8"))
src_cfg = json.loads(src.read_text(encoding="utf-8"))
cfg["allowed_data_lag_days"] = max(int(cfg.get("allowed_data_lag_days") or 0), 2)
cfg["target_data_lag_days"] = 2
if src_cfg.get("wecom_webhook"):
    cfg["wecom_webhook"] = src_cfg["wecom_webhook"]
# Prefer a readable local template if WeCom sandbox path is used.
tpl = str(cfg.get("template_image") or "")
if "Containers/com.tencent.WeWorkMac" in tpl:
    local_tpl = Path.home() / "Library/Application Support/profit-push/outputs/每日利润播报-2026-06-23.jpg"
    if local_tpl.exists():
        cfg["template_image"] = str(local_tpl)
root.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("config lag days =>", cfg["allowed_data_lag_days"], cfg["target_data_lag_days"])
print("webhook configured =>", bool(cfg.get("wecom_webhook")))
PY

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

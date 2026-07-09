#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$ROOT/config.json"

echo "请输入企业微信机器人 webhook（输入时不会显示）："
read -r -s WEBHOOK
echo

if [[ -z "$WEBHOOK" || "$WEBHOOK" != https://qyapi.weixin.qq.com/cgi-bin/webhook/send\?key=* ]]; then
  echo "Webhook 格式不正确，已取消。"
  exit 1
fi

python3 - "$CONFIG" "$WEBHOOK" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
webhook = sys.argv[2]
data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
data["wecom_webhook"] = webhook
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

chmod 600 "$CONFIG"
echo "已保存到本机配置：$CONFIG"

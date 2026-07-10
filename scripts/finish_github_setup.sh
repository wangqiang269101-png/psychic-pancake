#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GH="$ROOT/.tools/gh_2.96.0_macOS_arm64/bin/gh"
REPO="wangqiang269101-png/psychic-pancake"
POWERBI_URL="${POWERBI_REPORT_URL:-https://app.powerbi.com/reportEmbed?reportId=aa5bfe1a-b218-4be5-83e3-ce61bfd8d6c2&autoAuth=true&ctid=7c792a97-2300-4444-aa97-172fed9b0501}"

cd "$ROOT"

if [[ ! -x "$GH" ]]; then
  echo "缺少 gh 二进制: $GH"
  exit 1
fi

if ! "$GH" auth status >/dev/null 2>&1; then
  echo "GitHub 未登录。请先执行:"
  echo "  $GH auth login --web --hostname github.com --git-protocol https"
  exit 1
fi

if [[ -z "${WECOM_WEBHOOK:-}" ]]; then
  echo "请设置环境变量 WECOM_WEBHOOK 后再运行本脚本。"
  exit 1
fi

echo "==> push main"
git push -u origin main

echo "==> configure secrets/vars"
"$GH" secret set WECOM_WEBHOOK --repo "$REPO" --body "$WECOM_WEBHOOK"
if [[ -n "${PROFIT_DATA_JSON_URL:-}" ]]; then
  "$GH" secret set PROFIT_DATA_JSON_URL --repo "$REPO" --body "$PROFIT_DATA_JSON_URL"
fi
if [[ -n "${PROFIT_DATA_JSON_HEADERS:-}" ]]; then
  "$GH" secret set PROFIT_DATA_JSON_HEADERS --repo "$REPO" --body "$PROFIT_DATA_JSON_HEADERS"
fi
"$GH" variable set POWERBI_REPORT_URL --repo "$REPO" --body "$POWERBI_URL"

SOURCE_MODE="${SOURCE_MODE:-auto}"
echo "==> workflow dry_run (${SOURCE_MODE})"
"$GH" workflow run daily-profit-push.yml --repo "$REPO" -f "source_mode=${SOURCE_MODE}" -f dry_run=true
sleep 20
RUN_ID="$("$GH" run list --repo "$REPO" --workflow daily-profit-push.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
echo "Dry run: https://github.com/${REPO}/actions/runs/${RUN_ID}"
"$GH" run watch "$RUN_ID" --repo "$REPO" --exit-status

echo "==> workflow real push (${SOURCE_MODE})"
"$GH" workflow run daily-profit-push.yml --repo "$REPO" -f "source_mode=${SOURCE_MODE}" -f dry_run=false
sleep 10
RUN_ID="$("$GH" run list --repo "$REPO" --workflow daily-profit-push.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
echo "Real run: https://github.com/${REPO}/actions/runs/${RUN_ID}"
"$GH" run watch "$RUN_ID" --repo "$REPO" --exit-status

echo "Setup complete."

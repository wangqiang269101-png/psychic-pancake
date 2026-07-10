# 每日利润云端推送（GitHub Actions）

本目录提供**不依赖本机在线**的利润播报方案：由 GitHub Actions 全天约每 1.5 小时检查一次（北京时间 00:00–22:30，所有日期），数据更新后自动推送企业微信（文本 + 图片）。

调度（UTC cron，对应北京时间）：

- `0 1,4,7,10,13,16,19,22 * * *` → 北京 00:00 / 03:00 / 06:00 / 09:00 / 12:00 / 15:00 / 18:00 / 21:00
- `30 2,5,8,11,14,17,20,23 * * *` → 北京 01:30 / 04:30 / 07:30 / 10:30 / 13:30 / 16:30 / 19:30 / 22:30

## 能力说明

- 数据抓取：
  - `http_json`（推荐）：从可访问的 JSON API 拉取利润数据
  - `powerbi_scrape`（备选）：无 API 时，尝试从 Power BI 页面抓取网络 JSON 响应
  - `auto`：优先 `http_json`，失败后回退 `powerbi_scrape`
- 消息发送：
  - 企业微信 `markdown` 文本
  - 企业微信 `image`（`base64 + md5`）
- 去重与防重复：
  - 对“数据日期 + 关键利润值 + 区域明细”计算指纹
  - 指纹未变化则跳过推送
- 稳定性：
  - HTTP 和企业微信发送均有指数退避重试
  - 关键日志输出到 Actions 日志
  - 状态文件通过 Actions cache 持久化

## 数据格式要求

支持以下结构之一：

- `results[0].tables[0].rows`
- `rows`
- `value`
- 或直接是行数组

行中必须包含“公司”汇总行（`类型=公司`），并包含字段：

- `日期`
- `当日总利润` / `当日外卖利润` / `当日团购利润`
- `月累计总利润` / `月累计外卖利润` / `月累计团购利润`
- 可选：`本月预计总利润` / `本月预计外卖利润` / `本月预计团购利润`

另需至少一行 `类型=区域` 数据用于区域榜单。

## GitHub 配置

工作流文件：`.github/workflows/daily-profit-push.yml`

### 必填 Secrets

- `WECOM_WEBHOOK`：企业微信机器人 webhook
- `PROFIT_DATA_JSON_URL`：利润数据 JSON 接口地址（推荐）

### 可选 Secrets

- `PROFIT_DATA_JSON_HEADERS`：JSON 字符串格式请求头，例如：
  - `{"Authorization":"Bearer xxx","x-api-key":"xxx"}`

### 可选 Repository Variables

- `POWERBI_REPORT_URL`（默认已内置）
- `TARGET_DATA_LAG_DAYS`（默认 `2`）
- `MAX_DATA_LAG_DAYS`（默认 `2`）
- `WECOM_MAX_ATTEMPTS`（默认 `4`）
- `WECOM_TIMEOUT_SECONDS`（默认 `30`）
- `WECOM_RETRY_BASE_SECONDS`（默认 `1.5`）

## 首次上线步骤

1. 将 `cloud_push` 目录与 workflow 推送到仓库默认分支。
2. 在仓库 Settings -> Secrets and variables 中配置上面参数。
3. 手动触发 workflow（`workflow_dispatch`）先执行一次 `dry_run=true`。
4. 查看 artifact 中生成的 `profit-report-*.png` 和 `message-*.md`。
5. 再手动触发一次 `dry_run=false` 完成真实推送验证。

## 本地验证

安装依赖：

```bash
python3 -m pip install -r cloud_push/requirements.txt
```

样例 dry-run（不发企业微信）：

```bash
python3 cloud_push/profit_cloud_push.py --source sample --dry-run --force
```

真实发送示例（需要环境变量）：

```bash
export WECOM_WEBHOOK="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx"
export PROFIT_DATA_JSON_URL="https://your-api/profit/latest.json"
python3 cloud_push/profit_cloud_push.py --source http_json
```

## 故障排查

- `missing PROFIT_DATA_JSON_URL`：
  - 未配置数据接口，补充 `PROFIT_DATA_JSON_URL`。
- `report date is ... but expected ...`：
  - 数据尚未更新到目标日期，脚本会自动跳过。
- `payload fingerprint unchanged`：
  - 数据未变化，去重逻辑生效。
- `WeCom send failed`：
  - 检查 webhook 是否有效、机器人是否开启、网络是否可达。
- `powerbi_scrape` 失败：
  - GitHub 环境中 Power BI 认证通常受限，建议改用 `http_json` 稳定接口。

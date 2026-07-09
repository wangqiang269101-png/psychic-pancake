# 每日利润播报｜本机定时版

本机版规则：

- 每天 08:00 尝试推送。
- 如果 08:00 电脑没开机/休眠，电脑开机或唤醒后由 5 分钟巡检补发。
- 每个数据日期只发送一次，防止重复推送。
- 正式推送只发送 T-2（前天）数据；如果 Power BI 最新日期还不是前天，会自动跳过，避免误发未产出或旧数据。
- 样例数据只能用于 dry-run 验证，真实发送会被拒绝。
- 图片沿用已确认的黄色模板；图片不展示“本月预计”。
- 文字包含当日、本月累计、本月预计。

## 数据入口

安装后，把 Power BI 查询结果 JSON 放到运行目录：

`/Users/wangyongqiang/Library/Application Support/profit-push/data/latest-profit-payload.json`

支持两类结构：

- Power BI REST/Power Automate 原始返回：`results[0].tables[0].rows`
- 直接行数组：每行包含 `类型 / 日期 / 区域 / 当日总利润 / 当日外卖利润 / 当日团购利润 / 月累计总利润 / 月累计外卖利润 / 月累计团购利润`

如果要在图片底部展示“单天亏损城市明细”，数据里需要额外包含城市行：

- `类型=城市`
- `城市`
- `区域`
- `当日总利润`
- 可选：`当日外卖利润 / 当日团购利润`

脚本会自动筛出 `当日总利润 < 0` 的城市，按亏损额从高到低展示前 6 个。

## 配置企业微信 webhook

复制配置模板：

```bash
cp "/Users/wangyongqiang/Library/Application Support/profit-push/config.example.json" \
   "/Users/wangyongqiang/Library/Application Support/profit-push/config.json"
```

然后运行隐藏输入工具：

```bash
"/Users/wangyongqiang/Library/Application Support/profit-push/set_webhook.sh"
```

## 安装本机计划任务

```bash
/Users/wangyongqiang/Desktop/codex/profit-cloud-deploy/local_push/install_launch_agent.sh
```

## 手动验证

只生成图片和文案，不发送：

```bash
"/Users/wangyongqiang/Library/Application Support/profit-push/run_profit_push.sh" \
  --force --dry-run \
  --data "/Users/wangyongqiang/Library/Application Support/profit-push/data/sample-profit-payload.json"
```

真实发送最新数据：

```bash
"/Users/wangyongqiang/Library/Application Support/profit-push/run_profit_push.sh" --force
```

注意：真实发送前必须把正式 Power BI 数据保存为
`/Users/wangyongqiang/Library/Application Support/profit-push/data/latest-profit-payload.json`，
且数据日期必须是 T-2。

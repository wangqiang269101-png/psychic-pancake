#!/usr/bin/env python3
"""Local daily profit report sender.

Runs safely under launchd:
- before 08:00: skip
- after 08:00: send once per report data date
- if Mac was off/asleep at 08:00: the 5-minute launchd interval catches up

Input data is a Power BI query payload saved as JSON at data/latest-profit-payload.json
unless overridden in config.json.
"""

from __future__ import annotations

import argparse
import base64
import calendar
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import textwrap
import traceback
import urllib.error
import urllib.request

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "outputs"
LOG_DIR = ROOT / "logs"
STATE_PATH = ROOT / "state.json"
CONFIG_PATH = ROOT / "config.json"
LOCK_PATH = ROOT / ".profit_push.lock"

DEFAULT_CONFIG = {
    "data_path": str(DATA_DIR / "latest-profit-payload.json"),
    "not_before": "08:00",
    "timezone_note": "Asia/Shanghai; this script uses the Mac local timezone.",
    "allowed_data_lag_days": 2,
    "target_data_lag_days": 2,
    "template_image": "/Users/wangyongqiang/Library/Containers/com.tencent.WeWorkMac/Data/tmp/wecom-temp-209980-ed3f71a0564cd5b7681eaff2063156a9.jpg",
    "wecom_webhook": "",
}


class SkipSend(Exception):
    """Expected no-op condition."""


class ConfigError(Exception):
    """Configuration is incomplete."""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="ignore time and sent-state guards")
    parser.add_argument("--dry-run", action="store_true", help="render output without sending to WeCom")
    parser.add_argument("--data", help="override input payload JSON path")
    parser.add_argument("--allow-sample-send", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    ensure_dirs()
    with lock_file():
        try:
            config = load_config()
            if args.data:
                config["data_path"] = args.data

            now = dt.datetime.now()
            if not args.force:
                enforce_time_window(now, config)

            payload_path = Path(config["data_path"]).expanduser()
            if is_sample_data_path(payload_path) and not args.dry_run and not args.allow_sample_send:
                raise ConfigError("拒绝真实发送样例数据。请使用正式 Power BI 数据文件 latest-profit-payload.json。")
            if not payload_path.exists():
                raise SkipSend(f"数据文件不存在：{payload_path}")

            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            report = normalize_profit_payload(payload)
            if is_known_sample_report(report) and not args.dry_run and not args.allow_sample_send:
                raise ConfigError("检测到样例利润数据，拒绝真实发送。请等待正式 Power BI 数据。")
            validate_profit_report(
                report,
                now,
                int(config.get("allowed_data_lag_days", config.get("target_data_lag_days", 2))),
                int(config.get("target_data_lag_days", 2)),
            )

            state = load_state()
            if not args.force and already_sent(state, report["date_text"]):
                raise SkipSend(f"数据日期 {report['date_text']} 已发送，跳过重复推送。")

            message = build_message_text(report)
            image_path = render_profit_image(report, Path(config["template_image"]).expanduser())
            log("rendered", {"image": str(image_path), "date": report["date_text"]})

            webhook = os.environ.get("WECOM_WEBHOOK") or str(config.get("wecom_webhook") or "").strip()
            if not webhook and not args.dry_run:
                raise ConfigError("缺少企业微信 webhook：请设置环境变量 WECOM_WEBHOOK 或填写 local_push/config.json。")

            if args.dry_run:
                preview_path = OUTPUT_DIR / f"message-{report['date_text']}.md"
                preview_path.write_text(message, encoding="utf-8")
                log("dry_run_ok", {"message": str(preview_path), "image": str(image_path)})
                return 0

            send_wecom_markdown(webhook, message)
            send_wecom_image(webhook, image_path)
            mark_sent(state, report["date_text"], now)
            save_state(state)
            log("sent", {"data_date": report["date_text"]})
            return 0

        except SkipSend as exc:
            log("skip", {"reason": str(exc)})
            return 0
        except Exception as exc:  # launchd log should capture details.
            log("error", {"error": str(exc), "traceback": traceback.format_exc()})
            return 1


def ensure_dirs() -> None:
    for path in [DATA_DIR, OUTPUT_DIR, LOG_DIR]:
        path.mkdir(parents=True, exist_ok=True)


@contextlib.contextmanager
def lock_file():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        config.update(loaded)
    return config


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"sent_data_dates": [], "history": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def enforce_time_window(now: dt.datetime, config: dict) -> None:
    hh, mm = [int(part) for part in str(config.get("not_before", "08:00")).split(":")[:2]]
    threshold = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < threshold:
        raise SkipSend(f"未到推送时间：当前 {now:%H:%M}，设定 {hh:02d}:{mm:02d}。")


def already_sent(state: dict, data_date: str) -> bool:
    return data_date in set(state.get("sent_data_dates", []))


def is_sample_data_path(path: Path) -> bool:
    try:
        name = path.name.lower()
    except Exception:
        return False
    return name.startswith("sample-") or "sample" in name


def config_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "n", "off", "否", "不"}


def approx(value: float, expected: float, eps: float = 0.005) -> bool:
    return abs(float(value) - expected) <= eps


def is_known_sample_report(report: dict) -> bool:
    """Reject the bundled example if it is accidentally copied into the live data path."""
    if report.get("date_text") != "2026-06-23":
        return False
    c = report.get("company") or {}
    company_matches = (
        approx(c.get("daily_total", float("nan")), 53.49)
        and approx(c.get("daily_delivery", float("nan")), 56.38)
        and approx(c.get("daily_group", float("nan")), -2.89)
        and approx(c.get("month_total", float("nan")), 444.40)
        and approx(c.get("month_delivery", float("nan")), 480.24)
        and approx(c.get("month_group", float("nan")), -35.84)
    )
    if not company_matches:
        return False
    sample_regions = {
        ("川藏一区", 116.35, 124.79, -8.43),
        ("川藏二区", 104.65, 110.31, -5.65),
        ("江西二区", 98.60, 101.98, -3.38),
    }
    actual_regions = {
        (row.get("name"), round(float(row.get("total", float("nan"))), 2), round(float(row.get("delivery", float("nan"))), 2), round(float(row.get("group", float("nan"))), 2))
        for row in report.get("regions", [])
    }
    return sample_regions.issubset(actual_regions)


def mark_sent(state: dict, data_date: str, now: dt.datetime) -> None:
    sent = list(dict.fromkeys([*state.get("sent_data_dates", []), data_date]))
    state["sent_data_dates"] = sent[-180:]
    history = list(state.get("history", []))
    history.append({"sent_at": now.isoformat(timespec="seconds"), "data_date": data_date})
    state["history"] = history[-180:]


def normalize_profit_payload(payload) -> dict:
    rows = payload
    if not isinstance(rows, list):
        rows = (
            (((payload or {}).get("results") or [{}])[0].get("tables") or [{}])[0].get("rows")
            or (payload or {}).get("firstTableRows")
            or (payload or {}).get("rows")
            or (payload or {}).get("value")
            or []
        )
    if not isinstance(rows, list):
        raise ValueError("Power BI 数据不是可识别的行数组。")

    normalized_rows = [normalize_row(row) for row in rows]
    company_row = next((row for row in normalized_rows if str(row.get("类型", "")).strip() == "公司"), None)
    if not company_row:
        raise ValueError("未找到“类型=公司”的汇总行。")

    report_date = parse_date(company_row.get("日期"))
    accounted_days = max(1, report_date.day)
    days_in_month = calendar.monthrange(report_date.year, report_date.month)[1]

    month_total = to_number(company_row.get("月累计总利润"))
    month_delivery = to_number(company_row.get("月累计外卖利润"))
    month_group = to_number(company_row.get("月累计团购利润"))

    regions = []
    loss_cities = []
    for row in normalized_rows:
        row_type = str(row.get("类型", "")).strip()
        if row_type == "区域" and row.get("区域"):
            regions.append({
                "name": str(row.get("区域")),
                "total": to_number(row.get("当日总利润")),
                "delivery": to_number(row.get("当日外卖利润")),
                "group": to_number(row.get("当日团购利润")),
            })
            continue

        city_name = row.get("城市") or row.get("城") or row.get("城市名称")
        if row_type == "城市" or city_name:
            total = to_number(row.get("当日总利润"))
            if city_name and math.isfinite(total) and total < 0:
                loss_cities.append({
                    "name": str(city_name),
                    "region": str(row.get("区域") or ""),
                    "total": total,
                    "delivery": to_number(row.get("当日外卖利润")),
                    "group": to_number(row.get("当日团购利润")),
                })
    regions.sort(key=lambda item: item["total"], reverse=True)
    loss_cities.sort(key=lambda item: item["total"])

    return {
        "date": report_date,
        "date_text": report_date.strftime("%Y-%m-%d"),
        "company": {
            "daily_total": to_number(company_row.get("当日总利润")),
            "daily_delivery": to_number(company_row.get("当日外卖利润")),
            "daily_group": to_number(company_row.get("当日团购利润")),
            "month_total": month_total,
            "month_delivery": month_delivery,
            "month_group": month_group,
            "forecast_total": number_or_fallback(company_row.get("本月预计总利润"), month_total / accounted_days * days_in_month),
            "forecast_delivery": number_or_fallback(company_row.get("本月预计外卖利润"), month_delivery / accounted_days * days_in_month),
            "forecast_group": number_or_fallback(company_row.get("本月预计团购利润"), month_group / accounted_days * days_in_month),
        },
        "regions": regions[:9],
        "loss_cities": loss_cities[:20],
    }


def normalize_row(row: dict) -> dict:
    out = {}
    for key, value in (row or {}).items():
        clean_key = re.sub(r"^.*\[", "", str(key))
        clean_key = re.sub(r"\]$", "", clean_key)
        clean_key = re.sub(r"^[\"']|[\"']$", "", clean_key)
        out[clean_key] = value
    return out


def validate_profit_report(report: dict, now: dt.datetime, allowed_lag_days: int, target_lag_days: int = 2) -> None:
    c = report["company"]
    required = [
        c["daily_total"],
        c["daily_delivery"],
        c["daily_group"],
        c["month_total"],
        c["month_delivery"],
        c["month_group"],
    ]
    if any(not math.isfinite(value) for value in required):
        raise ValueError("公司利润字段存在空值或非数字，停止发送。")
    if not report["regions"]:
        raise ValueError("未读取到区域利润数据。")

    today = now.date()
    lag = (today - report["date"].date()).days
    if lag < 0:
        raise ValueError(f"数据日期 {report['date_text']} 晚于今天，停止发送。")
    expected_date = today - dt.timedelta(days=target_lag_days)
    if report["date"].date() != expected_date:
        raise SkipSend(f"数据日期 {report['date_text']} 不是应推 T-{target_lag_days} 日期 {expected_date:%Y-%m-%d}，避免误发未产出或旧数据。")
    if lag > allowed_lag_days:
        raise SkipSend(f"数据日期 {report['date_text']} 已超过允许滞后 {allowed_lag_days} 天，避免误发旧数据。")


def build_message_text(report: dict) -> str:
    c = report["company"]
    return "\n".join([
        f"📊 **每日利润播报｜{report['date_text']}**",
        "",
        "🔥 **当日利润**",
        f"> 权责总利润：**{fmt(c['daily_total'])} 万元**",
        f"> 外卖利润：{fmt(c['daily_delivery'])} 万元",
        f"> 团购利润：{fmt(c['daily_group'])} 万元",
        "",
        "📅 **本月累计**",
        f"> 累计权责总利润：**{fmt(c['month_total'])} 万元**",
        f"> 累计外卖利润：{fmt(c['month_delivery'])} 万元",
        f"> 累计团购利润：{fmt(c['month_group'])} 万元",
        "",
        "🎯 **本月预计**",
        f"> 本月预计累计权责总利润：**{fmt(c['forecast_total'])} 万元**",
        f"> 累计预计权责外卖利润：{fmt(c['forecast_delivery'])} 万元",
        f"> 累计预计团购利润：{fmt(c['forecast_group'])} 万元",
    ])


def render_profit_image(report: dict, template_path: Path) -> Path:
    if not template_path.exists():
        raise FileNotFoundError(f"固定黄色图片模板不存在：{template_path}")

    image = Image.open(template_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    c = report["company"]

    def rect(box, fill, radius=0):
        if radius:
            draw.rounded_rectangle(box, radius=radius, fill=fill)
        else:
            draw.rectangle(box, fill=fill)

    def text_box(box, text, size=28, fill="#202020", bold=False, align="center", valign="middle"):
        font = font_for(size, bold=bold)
        draw_fitted_text(draw, box, str(text), font, fill, align=align, valign=valign)

    # Date
    rect((756, 50, 966, 124), "#FFFFFF", 24)
    text_box((770, 63, 954, 111), report["date_text"], 25)

    # Monthly company summary
    rect((48, 294, 503, 378), "#FFFFFF")
    text_box((55, 299, 485, 371), f"{fmt(c['month_total'])} 万元", 55, align="left")

    rect((573, 257, 932, 345), "#FFF5C9", 18)
    text_box((595, 268, 910, 298), "累计外卖利润", 20, "#555555")
    text_box((595, 298, 910, 338), f"{fmt(c['month_delivery'])} 万元", 27, signed_color(c["month_delivery"]), align="right")

    rect((573, 366, 932, 454), "#FCE8E2", 18)
    text_box((595, 377, 910, 407), "累计团购利润", 20, "#555555")
    text_box((595, 407, 910, 447), f"{fmt(c['month_group'])} 万元", 27, signed_color(c["month_group"]), align="right")

    # Daily KPI values
    daily = [
        ("daily_total", 52, "#202020"),
        ("daily_delivery", 371, signed_color(c["daily_delivery"])),
        ("daily_group", 692, signed_color(c["daily_group"])),
    ]
    for key, x, color in daily:
        rect((x, 644, x + 279, 762), "#FFFFFF")
        text_box((x + 16, 651, x + 263, 723), fmt(c[key]), 53, color)
        text_box((x + 16, 723, x + 263, 754), "万元", 19, "#666666")

    row_top = 919
    row_height = 50
    for idx in range(9):
        y = row_top + idx * row_height
        rank = idx + 1
        row = report["regions"][idx] if idx < len(report["regions"]) else {"name": "-", "total": 0, "delivery": 0, "group": 0}
        fill = "#FFFFFF" if idx % 2 == 0 else "#FFF7D6"
        badge_fill = "#EA4650" if rank >= 8 else "#FFC800"
        badge_text = "#FFFFFF" if rank >= 8 else "#202020"
        rect((51, y, 981, y + row_height), fill)
        rect((52, y + 5, 87, y + 45), badge_fill, 8)
        text_box((52, y + 5, 87, y + 45), str(rank), 17, badge_text, bold=True)
        text_box((94, y + 4, 414, y + 46), row["name"], 18, align="left")
        text_box((484, y + 4, 609, y + 46), fmt(row["total"]), 18, signed_color(row["total"], "#202020"), align="right")
        text_box((680, y + 4, 785, y + 46), fmt(row["delivery"]), 18, signed_color(row["delivery"]), align="right")
        text_box((874, y + 4, 961, y + 46), fmt(row["group"]), 18, signed_color(row["group"]), align="right")

    loss_cities = report.get("loss_cities") or []
    if loss_cities:
        rect((29, 1378, 995, 1500), "#FFF8D7", 18)
        rect((51, 1390, 973, 1424), "#202020", 10)
        text_box((67, 1394, 430, 1420), "单天亏损城市明细", 19, "#FFFFFF", bold=True, align="left")
        text_box((815, 1394, 955, 1420), "单位：万元", 15, "#FFFFFF", align="right")

        for idx, city in enumerate(loss_cities[:6]):
            col = idx // 3
            row_idx = idx % 3
            x = 62 + col * 475
            y = 1432 + row_idx * 22
            name = city["name"]
            if city.get("region"):
                name = f"{name}｜{city['region']}"
            text_box((x, y, x + 315, y + 20), name, 15, "#202020", align="left")
            text_box((x + 325, y, x + 405, y + 20), fmt(city["total"]), 15, signed_color(city["total"]), align="right")

        if len(loss_cities) > 6:
            text_box((890, 1484, 965, 1498), f"+{len(loss_cities) - 6}个", 12, "#666666", align="right")

    out = OUTPUT_DIR / f"每日利润播报-{report['date_text']}.jpg"
    image.save(out, format="JPEG", quality=92)
    return out


def font_for(size: int, bold: bool = False):
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size, index=0)
        except Exception:
            continue
    return ImageFont.load_default()


def draw_fitted_text(draw: ImageDraw.ImageDraw, box, text, font, fill, align="center", valign="middle") -> None:
    x1, y1, x2, y2 = box
    max_w = x2 - x1
    max_h = y2 - y1
    lines = wrap_text(draw, text, font, max_w)
    line_heights = []
    line_widths = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_widths.append(bbox[2] - bbox[0])
        line_heights.append(bbox[3] - bbox[1])
    total_h = sum(line_heights) + max(0, len(lines) - 1) * 4
    if valign == "middle":
        y = y1 + (max_h - total_h) / 2
    else:
        y = y1
    for line, width, height in zip(lines, line_widths, line_heights):
        if align == "left":
            x = x1
        elif align == "right":
            x = x2 - width
        else:
            x = x1 + (max_w - width) / 2
        draw.text((x, y), line, font=font, fill=fill)
        y += height + 4


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_w: int):
    text = str(text)
    if "\n" in text:
        return [line for part in text.split("\n") for line in wrap_text(draw, part, font, max_w)]
    if draw.textbbox((0, 0), text, font=font)[2] <= max_w:
        return [text]
    lines = []
    current = ""
    for char in text:
        trial = current + char
        if draw.textbbox((0, 0), trial, font=font)[2] <= max_w or not current:
            current = trial
        else:
            lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines


def send_wecom_markdown(webhook: str, content: str) -> None:
    send_wecom(webhook, {"msgtype": "markdown", "markdown": {"content": content}})


def send_wecom_image(webhook: str, image_path: Path) -> None:
    data = image_path.read_bytes()
    send_wecom(webhook, {
        "msgtype": "image",
        "image": {
            "base64": base64.b64encode(data).decode("ascii"),
            "md5": hashlib.md5(data).hexdigest(),
        },
    })


def send_wecom(webhook: str, payload: dict) -> None:
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"企业微信发送失败：HTTP {exc.code} {body[:500]}") from exc
    result = json.loads(body or "{}")
    if status != 200 or int(result.get("errcode", -1)) != 0:
        raise RuntimeError(f"企业微信发送失败：{result.get('errmsg') or body[:500]}")


def parse_date(value) -> dt.datetime:
    if isinstance(value, (int, float)):
        # Excel date serial fallback, rarely needed.
        return dt.datetime(1899, 12, 30) + dt.timedelta(days=float(value))
    text = str(value or "").strip().replace("/", "-")
    for fmt_str in ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"]:
        try:
            return dt.datetime.strptime(text[:19], fmt_str)
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析数据日期：{value}") from exc


def to_number(value) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value if value is not None else "").replace(",", "").strip()
    if not text:
        return float("nan")
    return float(text)


def number_or_fallback(value, fallback: float) -> float:
    number = to_number(value)
    return number if math.isfinite(number) else float(fallback)


def fmt(value: float) -> str:
    return f"{float(value):.2f}"


def signed_color(value: float, positive="#34A853") -> str:
    return "#D94B58" if float(value) < 0 else positive


def log(event: str, data: dict) -> None:
    ensure_dirs()
    record = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "event": event, **data}
    line = json.dumps(record, ensure_ascii=False)
    print(line)
    with (LOG_DIR / "profit_push.log").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


if __name__ == "__main__":
    raise SystemExit(main())

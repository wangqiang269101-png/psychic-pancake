#!/usr/bin/env python3
"""Cloud-ready daily profit push.

Features:
- Poll Power BI data source (HTTP JSON API preferred, report scraping fallback).
- Deduplicate by payload fingerprint to avoid duplicate pushes.
- Send WeCom markdown + image message with retries.
- Render a clear KPI image showing daily / month-to-date / forecast.
"""

from __future__ import annotations

import argparse
import base64
import calendar
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import random
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "outputs"
STATE_DIR = ROOT / ".state"
STATE_PATH = STATE_DIR / "state.json"
DEFAULT_SAMPLE_PATH = ROOT.parent / "local_push" / "data" / "sample-profit-payload.json"

DEFAULT_POWERBI_URL = (
    "https://app.powerbi.com/reportEmbed?"
    "reportId=aa5bfe1a-b218-4be5-83e3-ce61bfd8d6c2&autoAuth=true&ctid=7c792a97-2300-4444-aa97-172fed9b0501"
)


class SkipSend(Exception):
    """Expected no-op condition."""


def log(event: str, **payload: Any) -> None:
    record = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        **payload,
    }
    print(json.dumps(record, ensure_ascii=False), flush=True)


def ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cloud profit push")
    parser.add_argument(
        "--source",
        choices=["auto", "http_json", "powerbi_scrape", "sample"],
        default=os.getenv("PROFIT_SOURCE_MODE", "auto"),
        help="data source mode",
    )
    parser.add_argument("--dry-run", action="store_true", help="render only, don't send")
    parser.add_argument("--force", action="store_true", help="ignore dedupe and date guard")
    parser.add_argument(
        "--sample-file",
        default=os.getenv("PROFIT_SAMPLE_PATH", str(DEFAULT_SAMPLE_PATH)),
        help="sample payload path for dry run",
    )
    return parser.parse_args()


def config_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return int(raw)


def config_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return float(raw)


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"history": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_extra_headers() -> dict[str, str]:
    raw = os.getenv("PROFIT_DATA_JSON_HEADERS", "").strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("PROFIT_DATA_JSON_HEADERS 必须是 JSON 对象。")
    return {str(k): str(v) for k, v in parsed.items()}


def request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_payload: dict | None = None,
    timeout: int = 30,
    max_attempts: int = 4,
    base_sleep: float = 1.5,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                json=json_payload,
                timeout=timeout,
            )
            if response.status_code >= 500:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:400]}")
            return response
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= max_attempts:
                break
            sleep_s = base_sleep * (2 ** (attempt - 1)) + random.uniform(0, 0.4)
            log("retrying_http", url=url, attempt=attempt, sleep_s=round(sleep_s, 2), error=str(exc))
            time.sleep(sleep_s)
    raise RuntimeError(f"请求失败：{url}，错误：{last_error}") from last_error


def fetch_http_json_payload() -> Any:
    url = os.getenv("PROFIT_DATA_JSON_URL", "").strip()
    if not url:
        raise ValueError("缺少 PROFIT_DATA_JSON_URL。")
    headers = {"Accept": "application/json", **parse_extra_headers()}
    log("fetch_http_json_start", url=url)
    response = request_with_retry("GET", url, headers=headers, timeout=35)
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"HTTP 失败：{response.status_code} {response.text[:500]}")
    return response.json()


def scrape_powerbi_payload(report_url: str) -> Any:
    log("powerbi_scrape_start", report_url=report_url)
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("缺少 playwright 依赖，请安装 requirements 或切换到 http_json 模式。") from exc

    candidates: list[Any] = []

    def maybe_collect_json(obj: Any) -> None:
        if isinstance(obj, (dict, list)):
            candidates.append(obj)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()

        def on_response(resp):
            try:
                ctype = (resp.headers or {}).get("content-type", "")
                if "json" not in ctype.lower():
                    return
                data = resp.json()
                maybe_collect_json(data)
            except Exception:
                return

        page.on("response", on_response)
        try:
            page.goto(report_url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(22000)
        except PlaywrightTimeoutError:
            log("powerbi_scrape_timeout", note="继续尝试解析已捕获请求")
        finally:
            browser.close()

    if not candidates:
        raise RuntimeError("未从 Power BI 页面捕获到 JSON 响应。")

    best_rows = None
    best_score = -1
    for item in candidates:
        for rows in extract_candidate_rows(item):
            score = score_rows(rows)
            if score > best_score:
                best_score = score
                best_rows = rows
    if not best_rows:
        raise RuntimeError("已捕获响应，但未识别到利润行数据结构。请改用 http_json 模式。")
    log("powerbi_scrape_ok", candidate_rows=len(best_rows), score=best_score)
    return best_rows


def extract_candidate_rows(obj: Any) -> list[list[dict]]:
    found: list[list[dict]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            if node and all(isinstance(x, dict) for x in node):
                found.append(node)
            for item in node:
                walk(item)
            return
        if isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(obj)
    return found


def score_rows(rows: list[dict]) -> int:
    if not rows:
        return -1
    score = 0
    for row in rows[:50]:
        clean = normalize_row(row)
        keys = set(clean.keys())
        if "类型" in keys:
            score += 2
        if "日期" in keys:
            score += 3
        if "当日总利润" in keys:
            score += 3
        if "月累计总利润" in keys:
            score += 3
        if "区域" in keys:
            score += 1
    return score


def fetch_payload(mode: str, sample_file: str) -> Any:
    if mode == "sample":
        return json.loads(Path(sample_file).read_text(encoding="utf-8"))
    if mode == "http_json":
        return fetch_http_json_payload()
    if mode == "powerbi_scrape":
        return scrape_powerbi_payload(os.getenv("POWERBI_REPORT_URL", DEFAULT_POWERBI_URL))

    # auto mode: try API first, then scrape fallback.
    try:
        return fetch_http_json_payload()
    except Exception as exc:  # noqa: BLE001
        log("fetch_http_json_failed", error=str(exc))
    return scrape_powerbi_payload(os.getenv("POWERBI_REPORT_URL", DEFAULT_POWERBI_URL))


def parse_date(value: Any) -> dt.datetime:
    if isinstance(value, (int, float)):
        return dt.datetime(1899, 12, 30) + dt.timedelta(days=float(value))
    text = str(value or "").strip().replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(text[:19], fmt)
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析日期: {value}") from exc


def to_number(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value if value is not None else "").replace(",", "").strip()
    if not text:
        return float("nan")
    return float(text)


def number_or_fallback(value: Any, fallback: float) -> float:
    out = to_number(value)
    return out if math.isfinite(out) else float(fallback)


def normalize_row(row: dict) -> dict:
    out: dict[str, Any] = {}
    for key, value in (row or {}).items():
        clean_key = re.sub(r"^.*\[", "", str(key))
        clean_key = re.sub(r"\]$", "", clean_key)
        clean_key = re.sub(r"^[\"']|[\"']$", "", clean_key)
        out[clean_key] = value
    return out


def normalize_profit_payload(payload: Any) -> dict:
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
        raise ValueError("数据不是可识别的行数组。")

    normalized_rows = [normalize_row(row) for row in rows]
    company_row = next((r for r in normalized_rows if str(r.get("类型", "")).strip() == "公司"), None)
    if not company_row:
        raise ValueError("未找到 类型=公司 汇总行。")

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
            regions.append(
                {
                    "name": str(row.get("区域")),
                    "total": to_number(row.get("当日总利润")),
                    "delivery": to_number(row.get("当日外卖利润")),
                    "group": to_number(row.get("当日团购利润")),
                }
            )
            continue

        city_name = row.get("城市") or row.get("城") or row.get("城市名称")
        if row_type == "城市" or city_name:
            total = to_number(row.get("当日总利润"))
            if city_name and math.isfinite(total) and total < 0:
                loss_cities.append(
                    {
                        "name": str(city_name),
                        "region": str(row.get("区域") or ""),
                        "total": total,
                        "delivery": to_number(row.get("当日外卖利润")),
                        "group": to_number(row.get("当日团购利润")),
                    }
                )
    regions.sort(key=lambda x: x["total"], reverse=True)
    loss_cities.sort(key=lambda x: x["total"])

    rise_regions = [item for item in regions if math.isfinite(item["total"]) and item["total"] > 0][:8]
    if not rise_regions:
        rise_regions = regions[:8]

    fall_regions = sorted(regions, key=lambda x: x["total"])
    negative_fall_regions = [item for item in fall_regions if math.isfinite(item["total"]) and item["total"] < 0][:8]
    if negative_fall_regions:
        fall_regions = negative_fall_regions
    else:
        fall_regions = fall_regions[:8]

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
            "forecast_delivery": number_or_fallback(
                company_row.get("本月预计外卖利润"), month_delivery / accounted_days * days_in_month
            ),
            "forecast_group": number_or_fallback(company_row.get("本月预计团购利润"), month_group / accounted_days * days_in_month),
        },
        "regions": regions[:12],
        "region_rise": rise_regions,
        "region_fall": fall_regions,
        "loss_cities": loss_cities[:12],
    }


def validate_profit_report(
    report: dict,
    *,
    now: dt.datetime,
    target_lag_days: int,
    max_lag_days: int,
    enforce_date_guard: bool = True,
) -> None:
    c = report["company"]
    required = [
        c["daily_total"],
        c["daily_delivery"],
        c["daily_group"],
        c["month_total"],
        c["month_delivery"],
        c["month_group"],
    ]
    if any(not math.isfinite(v) for v in required):
        raise ValueError("利润字段存在空值或非数字。")
    if not report["regions"]:
        raise ValueError("未读取到区域利润数据。")

    if enforce_date_guard:
        lag_days = (now.date() - report["date"].date()).days
        if lag_days < 0:
            raise ValueError(f"数据日期 {report['date_text']} 晚于当前日期。")
        expected = now.date() - dt.timedelta(days=target_lag_days)
        if report["date"].date() != expected:
            raise SkipSend(f"当前数据日期 {report['date_text']}，尚未到应发日期 {expected:%Y-%m-%d}。")
        if lag_days > max_lag_days:
            raise SkipSend(f"数据日期 {report['date_text']} 过旧，停止推送。")


def fmt(value: float) -> str:
    return f"{float(value):.2f}"


def build_message_text(report: dict) -> str:
    c = report["company"]
    return "\n".join(
        [
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
        ]
    )


def pick_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    if bold:
        candidates = [c.replace("Regular", "Bold") for c in candidates] + candidates
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def draw_text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, size: int, color: str = "#111", bold: bool = False) -> None:
    draw.text(xy, text, font=pick_font(size, bold=bold), fill=color)


def draw_text_right(
    draw: ImageDraw.ImageDraw,
    right_x: int,
    y: int,
    text: str,
    size: int,
    color: str = "#111",
    bold: bool = False,
) -> None:
    font = pick_font(size, bold=bold)
    content = str(text)
    bbox = draw.textbbox((0, 0), content, font=font)
    draw.text((right_x - (bbox[2] - bbox[0]), y), content, font=font, fill=color)


def fit_text(draw: ImageDraw.ImageDraw, text: str, max_width: int, size: int, bold: bool = False) -> str:
    value = str(text)
    font = pick_font(size, bold=bold)
    if draw.textbbox((0, 0), value, font=font)[2] <= max_width:
        return value
    ellipsis = "..."
    while value and draw.textbbox((0, 0), value + ellipsis, font=font)[2] > max_width:
        value = value[:-1]
    return (value + ellipsis) if value else ellipsis


def signed_color(value: float, positive: str = "#1A7F37") -> str:
    return "#C62828" if float(value) < 0 else positive


def render_profit_image(report: dict) -> Path:
    c = report["company"]
    image = Image.new("RGB", (1500, 1280), "#F4F6FA")
    draw = ImageDraw.Draw(image)
    colors = {
        "surface": "#FFFFFF",
        "border": "#DEE4EE",
        "headline": "#0F172A",
        "subtle": "#64748B",
        "muted_bg": "#EFF3FA",
        "primary": "#2A5BD7",
        "growth": "#1B8F4B",
        "risk": "#D14343",
        "row_alt": "#F8FAFD",
    }

    page_left = 36
    page_right = 1464

    # Header
    draw.rounded_rectangle((page_left, 28, page_right, 152), radius=24, fill=colors["surface"], outline=colors["border"], width=2)
    draw_text(draw, (68, 58), "每日利润播报", 44, colors["headline"], bold=True)
    draw_text(draw, (68, 108), "利润业务经营看板（单位：万元）", 22, colors["subtle"])
    draw.rounded_rectangle((1188, 58, 1432, 122), radius=16, fill=colors["muted_bg"])
    draw_text(draw, (1220, 80), report["date_text"], 28, colors["primary"], bold=True)

    # KPI cards
    card_top = 180
    card_height = 252
    card_gap = 22
    card_width = int((page_right - page_left - card_gap * 2) / 3)
    blocks = [
        ("当日利润", c["daily_total"], c["daily_delivery"], c["daily_group"]),
        ("本月累计", c["month_total"], c["month_delivery"], c["month_group"]),
        ("本月预计", c["forecast_total"], c["forecast_delivery"], c["forecast_group"]),
    ]
    for idx, (title, total, delivery, group) in enumerate(blocks):
        x1 = page_left + idx * (card_width + card_gap)
        x2 = x1 + card_width
        draw.rounded_rectangle((x1, card_top, x2, card_top + card_height), radius=20, fill=colors["surface"], outline=colors["border"], width=2)
        draw_text(draw, (x1 + 22, card_top + 22), title, 28, colors["headline"], bold=True)
        draw_text(draw, (x1 + 22, card_top + 70), "权责总利润", 20, colors["subtle"])
        draw_text_right(draw, x2 - 22, card_top + 66, f"{fmt(total)} 万元", 40, signed_color(total, colors["growth"]), bold=True)
        draw.line((x1 + 20, card_top + 128, x2 - 20, card_top + 128), fill="#E8EDF5", width=2)
        draw_text(draw, (x1 + 22, card_top + 150), "外卖利润", 20, colors["subtle"])
        draw_text_right(draw, x2 - 22, card_top + 150, f"{fmt(delivery)} 万元", 24, signed_color(delivery, colors["growth"]), bold=True)
        draw_text(draw, (x1 + 22, card_top + 194), "团购利润", 20, colors["subtle"])
        draw_text_right(draw, x2 - 22, card_top + 194, f"{fmt(group)} 万元", 24, signed_color(group, colors["growth"]), bold=True)

    # Detail section
    detail_top = 458
    detail_bottom = 1244
    draw.rounded_rectangle(
        (page_left, detail_top, page_right, detail_bottom),
        radius=20,
        fill=colors["surface"],
        outline=colors["border"],
        width=2,
    )
    draw_text(draw, (66, 488), "区域涨跌与亏损城市明细", 32, colors["headline"], bold=True)
    draw_text(draw, (66, 530), "负数红色标识风险，正数绿色标识增长", 20, colors["subtle"])

    panel_top = 570
    panel_bottom = 1216
    panel_gap = 18
    panel_left = 56
    panel_width = int((page_right - panel_left - panel_gap * 2 - 36) / 3)

    def panel_x(index: int) -> tuple[int, int]:
        x1 = panel_left + index * (panel_width + panel_gap)
        return x1, x1 + panel_width

    def draw_panel_row_bg(x1: int, x2: int, y: int, idx: int) -> None:
        draw.rectangle((x1 + 10, y, x2 - 10, y + 62), fill=colors["row_alt"] if idx % 2 == 0 else "#FFFFFF")
        draw.line((x1 + 10, y + 62, x2 - 10, y + 62), fill="#EDF1F7", width=1)

    rise_rows = report.get("region_rise") or []
    fall_rows = report.get("region_fall") or []
    loss_rows = report.get("loss_cities") or []

    # Panel 1: rising regions
    r1x1, r1x2 = panel_x(0)
    draw.rounded_rectangle((r1x1, panel_top, r1x2, panel_bottom), radius=16, fill="#F8FCF8", outline="#D9ECDC", width=2)
    draw.rounded_rectangle((r1x1 + 10, panel_top + 12, r1x2 - 10, panel_top + 58), radius=10, fill="#E8F6EC")
    draw_text(draw, (r1x1 + 22, panel_top + 24), "区域上涨", 24, colors["growth"], bold=True)
    draw_text(draw, (r1x1 + 22, panel_top + 84), "区域", 20, colors["subtle"], bold=True)
    draw_text_right(draw, r1x2 - 22, panel_top + 84, "当日利润", 20, colors["subtle"], bold=True)
    for idx in range(8):
        y = panel_top + 120 + idx * 62
        draw_panel_row_bg(r1x1, r1x2, y, idx)
        row = rise_rows[idx] if idx < len(rise_rows) else None
        if not row:
            continue
        name = fit_text(draw, row["name"], r1x2 - r1x1 - 170, 21)
        draw_text(draw, (r1x1 + 24, y + 18), name, 21, colors["headline"])
        draw_text_right(draw, r1x2 - 24, y + 18, fmt(row["total"]), 21, signed_color(row["total"], colors["growth"]), bold=True)

    # Panel 2: falling regions
    r2x1, r2x2 = panel_x(1)
    draw.rounded_rectangle((r2x1, panel_top, r2x2, panel_bottom), radius=16, fill="#FDF8F8", outline="#F0DADA", width=2)
    draw.rounded_rectangle((r2x1 + 10, panel_top + 12, r2x2 - 10, panel_top + 58), radius=10, fill="#FCECEC")
    draw_text(draw, (r2x1 + 22, panel_top + 24), "区域下滑", 24, colors["risk"], bold=True)
    draw_text(draw, (r2x1 + 22, panel_top + 84), "区域", 20, colors["subtle"], bold=True)
    draw_text_right(draw, r2x2 - 22, panel_top + 84, "当日利润", 20, colors["subtle"], bold=True)
    for idx in range(8):
        y = panel_top + 120 + idx * 62
        draw_panel_row_bg(r2x1, r2x2, y, idx)
        row = fall_rows[idx] if idx < len(fall_rows) else None
        if not row:
            continue
        name = fit_text(draw, row["name"], r2x2 - r2x1 - 170, 21)
        draw_text(draw, (r2x1 + 24, y + 18), name, 21, colors["headline"])
        draw_text_right(draw, r2x2 - 24, y + 18, fmt(row["total"]), 21, signed_color(row["total"], colors["growth"]), bold=True)

    # Panel 3: loss cities
    r3x1, r3x2 = panel_x(2)
    draw.rounded_rectangle((r3x1, panel_top, r3x2, panel_bottom), radius=16, fill="#F9FAFC", outline=colors["border"], width=2)
    draw.rounded_rectangle((r3x1 + 10, panel_top + 12, r3x2 - 10, panel_top + 58), radius=10, fill="#EEF3FA")
    draw_text(draw, (r3x1 + 22, panel_top + 24), "亏损城市", 24, colors["primary"], bold=True)
    draw_text(draw, (r3x1 + 18, panel_top + 84), "城市", 20, colors["subtle"], bold=True)
    draw_text(draw, (r3x1 + 160, panel_top + 84), "区域", 20, colors["subtle"], bold=True)
    draw_text_right(draw, r3x2 - 22, panel_top + 84, "当日利润", 20, colors["subtle"], bold=True)
    for idx in range(8):
        y = panel_top + 120 + idx * 62
        draw_panel_row_bg(r3x1, r3x2, y, idx)
        row = loss_rows[idx] if idx < len(loss_rows) else None
        if not row:
            continue
        city_name = fit_text(draw, row["name"], 128, 20)
        region_name = fit_text(draw, row.get("region", "-") or "-", 130, 20)
        draw_text(draw, (r3x1 + 18, y + 19), city_name, 20, colors["headline"])
        draw_text(draw, (r3x1 + 160, y + 19), region_name, 20, colors["subtle"])
        draw_text_right(draw, r3x2 - 22, y + 19, fmt(row["total"]), 21, signed_color(row["total"], colors["growth"]), bold=True)

    out = OUTPUT_DIR / f"profit-report-{report['date_text']}.png"
    image.save(out, format="PNG")
    return out


def compute_fingerprint(report: dict) -> str:
    digest_input = {
        "date": report["date_text"],
        "company": report["company"],
        "regions": report["regions"],
    }
    raw = json.dumps(digest_input, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def skip_if_duplicate(state: dict, fingerprint: str, *, force: bool) -> None:
    if force:
        return
    if state.get("last_fingerprint") == fingerprint:
        raise SkipSend("数据指纹未变化，跳过重复推送。")


def send_wecom(webhook: str, payload: dict) -> None:
    max_attempts = config_int("WECOM_MAX_ATTEMPTS", 4)
    timeout = config_int("WECOM_TIMEOUT_SECONDS", 30)
    backoff = config_float("WECOM_RETRY_BASE_SECONDS", 1.5)
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(webhook, json=payload, timeout=timeout)
            body_text = response.text
            parsed = {}
            if body_text:
                with contextlib.suppress(Exception):
                    parsed = response.json()
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}: {body_text[:500]}")
            if int((parsed or {}).get("errcode", -1)) != 0:
                raise RuntimeError(f"errcode={parsed.get('errcode')} errmsg={parsed.get('errmsg')}")
            return
        except Exception as exc:  # noqa: BLE001
            if attempt >= max_attempts:
                raise RuntimeError(f"企业微信发送失败: {exc}") from exc
            sleep_s = backoff * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
            log("retrying_wecom", attempt=attempt, sleep_s=round(sleep_s, 2), error=str(exc))
            time.sleep(sleep_s)


def send_wecom_markdown(webhook: str, content: str) -> None:
    send_wecom(webhook, {"msgtype": "markdown", "markdown": {"content": content}})


def send_wecom_image(webhook: str, image_path: Path) -> None:
    raw = image_path.read_bytes()
    send_wecom(
        webhook,
        {
            "msgtype": "image",
            "image": {
                "base64": base64.b64encode(raw).decode("ascii"),
                "md5": hashlib.md5(raw).hexdigest(),  # noqa: S324 - WeCom image API requires md5
            },
        },
    )


def run() -> int:
    args = parse_args()
    ensure_dirs()
    state = load_state()

    target_lag_days = config_int("TARGET_DATA_LAG_DAYS", 2)
    max_lag_days = config_int("MAX_DATA_LAG_DAYS", 2)
    webhook = os.getenv("WECOM_WEBHOOK", "").strip()

    try:
        payload = fetch_payload(args.source, args.sample_file)
        report = normalize_profit_payload(payload)
        validate_profit_report(
            report,
            now=dt.datetime.now(),
            target_lag_days=target_lag_days,
            max_lag_days=max_lag_days,
            enforce_date_guard=not args.force,
        )

        fingerprint = compute_fingerprint(report)
        skip_if_duplicate(state, fingerprint, force=args.force)

        message = build_message_text(report)
        image_path = render_profit_image(report)
        (OUTPUT_DIR / f"message-{report['date_text']}.md").write_text(message, encoding="utf-8")
        log("rendered", date=report["date_text"], image=str(image_path))

        if args.dry_run:
            log("dry_run_ok", mode=args.source, date=report["date_text"], fingerprint=fingerprint)
            return 0

        if not webhook:
            raise RuntimeError("缺少 WECOM_WEBHOOK 环境变量。")

        send_wecom_markdown(webhook, message)
        send_wecom_image(webhook, image_path)

        state["last_fingerprint"] = fingerprint
        state["last_sent_data_date"] = report["date_text"]
        history = list(state.get("history") or [])
        history.append(
            {
                "sent_at": dt.datetime.now().isoformat(timespec="seconds"),
                "data_date": report["date_text"],
                "fingerprint": fingerprint,
            }
        )
        state["history"] = history[-200:]
        save_state(state)
        log("sent_ok", date=report["date_text"], fingerprint=fingerprint)
        return 0
    except SkipSend as exc:
        log("skip", reason=str(exc))
        return 0
    except Exception as exc:  # noqa: BLE001
        log("error", error=str(exc), traceback=traceback.format_exc()[:4000])
        return 1


if __name__ == "__main__":
    sys.exit(run())

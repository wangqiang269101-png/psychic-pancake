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


def config_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


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
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from powerbi_scraper import scrape_powerbi_payload as scrape_from_dom

    rows = scrape_from_dom(report_url)
    company = next((row for row in rows if str(row.get("类型", "")).strip() == "公司"), None)
    log(
        "powerbi_auth_state",
        changed_in_memory=os.getenv("POWERBI_AUTH_STATE_CHANGED", "unknown"),
        origin_storage_present=os.getenv("POWERBI_AUTH_ORIGIN_STORAGE_PRESENT", "unknown"),
        persisted=False,
        reason="GitHub Actions cannot safely update repository secrets with GITHUB_TOKEN",
    )
    log(
        "powerbi_scrape_ok",
        candidate_rows=len(rows),
        date=(company or {}).get("日期"),
        regions=sum(1 for row in rows if str(row.get("类型", "")).strip() == "区域"),
        cities=sum(
            1
            for row in rows
            if str(row.get("类型", "")).strip() == "城市"
            or bool(row.get("城市") or row.get("城市名称") or row.get("外卖城市"))
        ),
    )
    return rows


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


def row_value(row: dict, *aliases: str) -> Any:
    for alias in aliases:
        if alias in row and row[alias] not in (None, ""):
            return row[alias]
    return None


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
    company_row = next(
        (
            row
            for row in normalized_rows
            if str(row_value(row, "类型", "数据类型", "层级") or "").strip() == "公司"
        ),
        None,
    )
    if not company_row:
        raise ValueError("未找到 类型=公司 汇总行。")

    report_date = parse_date(row_value(company_row, "日期", "数据日期", "统计日期"))
    accounted_days = max(1, report_date.day)
    days_in_month = calendar.monthrange(report_date.year, report_date.month)[1]

    month_total = to_number(row_value(company_row, "月累计总利润", "月累计权责总利润"))
    month_delivery = to_number(row_value(company_row, "月累计外卖利润", "月累计权责外卖利润"))
    month_group = to_number(row_value(company_row, "月累计团购利润"))

    regions = []
    city_detail_rows = 0
    loss_cities = []
    for row in normalized_rows:
        row_type = str(row_value(row, "类型", "数据类型", "层级") or "").strip()
        region_name = row_value(row, "区域", "区域名称", "大区")
        if row_type == "区域" and region_name:
            regions.append(
                {
                    "name": str(region_name),
                    "total": to_number(row_value(row, "当日总利润", "当日权责总利润", "权责总利润(万)", "权责总利润")),
                    "delivery": to_number(
                        row_value(row, "当日外卖利润", "当日权责外卖利润", "权责外卖利润(万)", "权责外卖利润")
                    ),
                    "group": to_number(row_value(row, "当日团购利润", "团购利润(万)", "团购利润")),
                }
            )
            continue

        city_name = row_value(row, "城市", "城", "城市名称", "外卖城市")
        if row_type == "城市" or city_name:
            city_detail_rows += 1
            total = to_number(row_value(row, "当日总利润", "当日权责总利润", "权责总利润(万)", "权责总利润"))
            if city_name and math.isfinite(total) and total < 0:
                loss_cities.append(
                    {
                        "name": str(city_name),
                        "region": str(region_name or ""),
                        "total": total,
                        "delivery": to_number(
                            row_value(row, "当日外卖利润", "当日权责外卖利润", "权责外卖利润(万)", "权责外卖利润")
                        ),
                        "group": to_number(row_value(row, "当日团购利润", "团购利润(万)", "团购利润")),
                    }
                )
    regions.sort(key=lambda x: x["total"], reverse=True)
    loss_cities.sort(key=lambda x: x["total"])

    profit_regions = [item for item in regions if math.isfinite(item["total"]) and item["total"] >= 0]
    loss_regions = sorted(
        [item for item in regions if math.isfinite(item["total"]) and item["total"] < 0],
        key=lambda x: x["total"],
    )

    return {
        "date": report_date,
        "date_text": report_date.strftime("%Y-%m-%d"),
        "company": {
            "daily_total": to_number(
                row_value(company_row, "当日总利润", "当日权责总利润", "权责总利润(万)", "权责总利润")
            ),
            "daily_delivery": to_number(
                row_value(company_row, "当日外卖利润", "当日权责外卖利润", "权责外卖利润(万)", "权责外卖利润")
            ),
            "daily_group": to_number(row_value(company_row, "当日团购利润", "团购利润(万)", "团购利润")),
            "month_total": month_total,
            "month_delivery": month_delivery,
            "month_group": month_group,
            "forecast_total": number_or_fallback(
                row_value(company_row, "本月预计总利润", "本月预计权责总利润"),
                month_total / accounted_days * days_in_month,
            ),
            "forecast_delivery": number_or_fallback(
                row_value(company_row, "本月预计外卖利润", "本月预计权责外卖利润"),
                month_delivery / accounted_days * days_in_month,
            ),
            "forecast_group": number_or_fallback(
                row_value(company_row, "本月预计团购利润"),
                month_group / accounted_days * days_in_month,
            ),
        },
        "regions": regions,
        "profit_regions": profit_regions,
        "loss_regions": loss_regions,
        "city_details_available": city_detail_rows > 0,
        "loss_cities": loss_cities,
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
    if config_bool("REQUIRE_CITY_DETAILS") and not report["city_details_available"]:
        raise ValueError("城市利润明细缺失：区域→城市下钻未生效，拒绝推送。")

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


def pick_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Load a real CJK font; never silently fall back to Pillow's tiny bitmap font."""
    regular = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    bold_candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    for path in (bold_candidates if bold else regular):
        try:
            # PingFang collection index 0 is a complete Simplified Chinese face.
            return ImageFont.truetype(path, size=size, index=0)
        except (OSError, ValueError):
            continue
    raise RuntimeError("未找到可用中文字体（PingFang / STHeiti / Noto Sans CJK / 文泉驿）。")


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
    """Render a phone-first portrait report for WeCom."""
    c = report["company"]
    profit_rows = report.get("profit_regions") or []
    region_loss_rows = report.get("loss_regions") or []
    loss_rows = report.get("loss_cities") or []

    width, margin, gap = 1080, 40, 24
    card_width = width - margin * 2
    header_h, kpi_h = 168, 256
    section_header_h, table_header_h, row_h = 86, 62, 70

    def section_height(rows: list[dict], empty_rows: int = 1) -> int:
        return section_header_h + table_header_h + max(len(rows), empty_rows) * row_h + 24

    content_height = (
        margin + header_h + gap
        + 3 * (kpi_h + gap)
        + section_height(profit_rows)
        + gap + section_height(region_loss_rows)
        + gap + section_height(loss_rows)
        + margin
    )
    image = Image.new("RGB", (width, content_height), "#F3F6FA")
    draw = ImageDraw.Draw(image)
    colors = {
        "surface": "#FFFFFF", "border": "#DDE4EE", "headline": "#14213D",
        "subtle": "#64748B", "primary": "#2463D4", "primary_bg": "#EAF1FF",
        "growth": "#168447", "growth_bg": "#E9F7EF",
        "risk": "#CE3D45", "risk_bg": "#FDEDEF", "row_alt": "#F8FAFD",
    }

    def card(y1: int, y2: int) -> None:
        draw.rounded_rectangle(
            (margin, y1, width - margin, y2), radius=24,
            fill=colors["surface"], outline=colors["border"], width=2,
        )

    # Header
    y = margin
    card(y, y + header_h)
    draw_text(draw, (margin + 32, y + 28), "每日利润播报", 54, colors["headline"], bold=True)
    draw_text(draw, (margin + 34, y + 101), "经营利润看板 · 单位：万元", 28, colors["subtle"])
    draw.rounded_rectangle((width - margin - 310, y + 46, width - margin - 30, y + 112), radius=18, fill=colors["primary_bg"])
    draw_text_right(draw, width - margin - 58, y + 60, report["date_text"], 32, colors["primary"], bold=True)
    y += header_h + gap

    # KPI cards form a vertical reading flow.
    blocks = [
        ("当日利润", c["daily_total"], c["daily_delivery"], c["daily_group"]),
        ("本月累计", c["month_total"], c["month_delivery"], c["month_group"]),
        ("本月预计", c["forecast_total"], c["forecast_delivery"], c["forecast_group"]),
    ]
    for title, total, delivery, group in blocks:
        card(y, y + kpi_h)
        draw.rounded_rectangle((margin + 22, y + 22, margin + 214, y + 78), radius=14, fill=colors["primary_bg"])
        draw_text(draw, (margin + 44, y + 33), title, 32, colors["primary"], bold=True)
        draw_text(draw, (margin + 34, y + 103), "权责总利润", 32, colors["headline"], bold=True)
        draw_text_right(draw, width - margin - 34, y + 91, f"{fmt(total)}", 48, signed_color(total, colors["growth"]), bold=True)
        draw_text_right(draw, width - margin - 34, y + 143, "万元", 25, colors["subtle"])
        draw.line((margin + 34, y + 178, width - margin - 34, y + 178), fill="#E8EDF4", width=2)
        draw_text(draw, (margin + 34, y + 199), "外卖", 30, colors["subtle"])
        draw_text(draw, (margin + 340, y + 199), "团购", 30, colors["subtle"])
        draw_text_right(draw, margin + 310, y + 194, fmt(delivery), 36, signed_color(delivery, colors["growth"]), bold=True)
        draw_text_right(draw, width - margin - 34, y + 194, fmt(group), 36, signed_color(group, colors["growth"]), bold=True)
        y += kpi_h + gap

    def draw_section(title: str, rows: list[dict], tone: str, city: bool = False) -> None:
        nonlocal y
        section_h = section_height(rows)
        card(y, y + section_h)
        accent = colors[tone]
        accent_bg = colors[f"{tone}_bg"]
        draw.rounded_rectangle((margin + 20, y + 20, width - margin - 20, y + 72), radius=14, fill=accent_bg)
        draw_text(draw, (margin + 40, y + 29), title, 34, accent, bold=True)
        draw_text_right(draw, width - margin - 42, y + 34, f"{len(rows)} 项" if rows else "—", 26, colors["subtle"])
        header_y = y + section_header_h
        draw.rectangle((margin + 20, header_y, width - margin - 20, header_y + table_header_h), fill="#F1F5F9")
        if city:
            draw_text(draw, (margin + 40, header_y + 15), "城市", 28, colors["subtle"], bold=True)
            draw_text(draw, (margin + 385, header_y + 15), "区域", 28, colors["subtle"], bold=True)
        else:
            draw_text(draw, (margin + 40, header_y + 15), "区域", 28, colors["subtle"], bold=True)
        draw_text_right(draw, width - margin - 42, header_y + 15, "当日利润", 28, colors["subtle"], bold=True)

        body_y = header_y + table_header_h
        if rows:
            for idx, row in enumerate(rows):
                row_y = body_y + idx * row_h
                if idx % 2 == 0:
                    draw.rectangle((margin + 20, row_y, width - margin - 20, row_y + row_h), fill=colors["row_alt"])
                if city:
                    draw_text(draw, (margin + 40, row_y + 17), fit_text(draw, row["name"], 300, 32), 32, colors["headline"])
                    draw_text(draw, (margin + 385, row_y + 17), fit_text(draw, row.get("region") or "—", 300, 30), 30, colors["subtle"])
                else:
                    draw_text(draw, (margin + 40, row_y + 17), fit_text(draw, row["name"], 610, 32), 32, colors["headline"])
                draw_text_right(draw, width - margin - 42, row_y + 14, fmt(row["total"]), 34, signed_color(row["total"], colors["growth"]), bold=True)
                draw.line((margin + 32, row_y + row_h, width - margin - 32, row_y + row_h), fill="#EDF1F6", width=1)
        else:
            message = "城市利润明细尚未抓取，下钻任务完成后自动展示" if city and not report.get("city_details_available") else (
                "当日无亏损城市" if city else f"当日无{title}"
            )
            color = colors["subtle"] if not report.get("city_details_available") else colors["growth"]
            draw_text(draw, (margin + 40, body_y + 18), message, 30, color, bold=True)
        y += section_h + gap

    draw_section("盈利区域", profit_rows, "growth")
    draw_section("亏损区域", region_loss_rows, "risk")
    draw_section("亏损城市", loss_rows, "primary", city=True)

    out = OUTPUT_DIR / f"profit-report-{report['date_text']}.png"
    image.save(out, format="PNG", optimize=True, compress_level=9)
    return out


def compute_fingerprint(report: dict) -> str:
    digest_input = {
        "date": report["date_text"],
        "company": report["company"],
        "regions": report["regions"],
        "city_details_available": report.get("city_details_available"),
        "loss_cities": report.get("loss_cities"),
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
        if args.source == "sample" and not args.dry_run and not args.force:
            raise RuntimeError("生产推送禁止使用 sample 数据源，请改用 auto/http_json/powerbi_scrape。")

        payload = fetch_payload(args.source, args.sample_file)
        report = normalize_profit_payload(payload)
        if args.source == "auto" and not report["city_details_available"]:
            log("http_json_missing_city_details", action="fallback_to_powerbi_scrape")
            payload = scrape_powerbi_payload(os.getenv("POWERBI_REPORT_URL", DEFAULT_POWERBI_URL))
            report = normalize_profit_payload(payload)
        if not report["city_details_available"]:
            log("city_details_unavailable", action="render_clear_placeholder")

        if args.source != "sample":
            latest_payload = ROOT / "data" / "latest-profit-payload.json"
            latest_payload.parent.mkdir(parents=True, exist_ok=True)
            latest_payload.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                encoding="utf-8",
            )
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

"""Scrape daily profit payload from the embedded Power BI report."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

REGION_NAMES = [
    "川藏一区",
    "川藏二区",
    "川藏三区",
    "江西一区",
    "江西二区",
    "湖北区域",
    "粤海区域",
    "河南区域",
    "福建&黔渝区域",
]

SKIP_LINES = {
    "Additional Conditional Formatting",
    "Select Row",
    "Scroll up",
    "Scroll down",
    "Scroll left",
    "Scroll right",
}


def load_browser_cookies() -> list[dict[str, str]]:
    try:
        import browser_cookie3
    except ImportError as exc:  # noqa: BLE001
        raise RuntimeError("缺少 browser-cookie3，请安装 cloud_push/requirements.txt。") from exc

    cookies: list[dict[str, str]] = []
    for cookie in browser_cookie3.chrome():
        if any(
            domain in cookie.domain
            for domain in ("powerbi.com", "microsoftonline.com", "analysis.windows.net", "microsoft.com")
        ):
            cookies.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path or "/",
                }
            )
    if not cookies:
        raise RuntimeError("未从 Chrome 读取到 Power BI 登录 Cookie，请先在 Chrome 登录 Power BI。")
    return cookies


def parse_aria_number(label: str, marker: str) -> float:
    match = re.search(rf"{re.escape(marker)}\s*(-?\d+(?:\.\d+)?)", label)
    if not match:
        raise ValueError(f"无法从 aria-label 解析 {marker}: {label}")
    return float(match.group(1))


def parse_aria_date(label: str) -> str:
    match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", label)
    if not match:
        raise ValueError(f"无法从 aria-label 解析日期: {label}")
    month, day, year = match.group(1).split("/")
    return f"{year}-{int(month):02d}-{int(day):02d}"


def is_number_line(text: str) -> bool:
    cleaned = text.replace(",", "").strip()
    return bool(re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned))


def to_number_line(text: str) -> float:
    return float(text.replace(",", "").strip())


def row_profit_triplet(numbers: list[float]) -> tuple[float, float, float]:
    if len(numbers) < 11:
        raise ValueError(f"行内数字不足，无法解析利润三列: {numbers}")
    return numbers[0], numbers[1], numbers[10]


def extract_row_numbers(lines: list[str]) -> list[float]:
    numbers: list[float] = []
    for line in lines:
        text = line.strip()
        if not text or text in SKIP_LINES or text.endswith("%"):
            continue
        if is_number_line(text):
            numbers.append(to_number_line(text))
    return numbers


def parse_region_rows(lines: list[str]) -> list[dict[str, Any]]:
    regions: list[dict[str, Any]] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx].strip()
        if line not in REGION_NAMES:
            idx += 1
            continue
        row_lines: list[str] = []
        idx += 1
        while idx < len(lines):
            candidate = lines[idx].strip()
            if candidate in REGION_NAMES or candidate == "Total" or re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", candidate):
                break
            row_lines.append(candidate)
            idx += 1
        numbers = extract_row_numbers(row_lines)
        if len(numbers) < 11:
            continue
        total, delivery, group = row_profit_triplet(numbers)
        regions.append(
            {
                "类型": "区域",
                "区域": line,
                "当日总利润": total,
                "当日外卖利润": delivery,
                "当日团购利润": group,
            }
        )
    return regions


def parse_monthly_totals(lines: list[str]) -> tuple[float, float, float]:
    try:
        start = next(i for i, line in enumerate(lines) if "财务利润-权责(月累计)" in line)
    except StopIteration as exc:
        raise ValueError("未找到月累计表格。") from exc

    for idx in range(len(lines) - 1, start, -1):
        if lines[idx].strip() != "Total":
            continue
        numbers = extract_row_numbers(lines[idx + 1 : idx + 30])
        if len(numbers) < 11:
            break
        return row_profit_triplet(numbers)
    raise ValueError("未解析到月累计 Total 行。")


def scrape_powerbi_payload(report_url: str) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # noqa: BLE001
        raise RuntimeError("缺少 playwright，请安装 cloud_push/requirements.txt。") from exc

    cookies = load_browser_cookies()
    aria_labels: list[str] = []
    body_text = ""

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1920, "height": 1400})
        context.add_cookies(cookies)
        page = context.new_page()
        page.goto(report_url, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(90000)
        for label in ("T-1财务利润", "T-1"):
            try:
                locator = page.get_by_text(label, exact=False).first
                if locator.count() > 0:
                    locator.click(timeout=3000)
                    page.wait_for_timeout(12000)
                    break
            except Exception:
                pass
        body_text = page.inner_text("body")
        for element in page.locator("[aria-label]").all():
            try:
                label = element.get_attribute("aria-label")
                if label:
                    aria_labels.append(label)
            except Exception:
                continue
        browser.close()

    daily_total = daily_delivery = daily_group = None
    date_text = None
    for label in aria_labels:
        if "权责总利润(万)" in label and daily_total is None:
            daily_total = parse_aria_number(label, "权责总利润(万)")
        elif "权责外卖利润(万)" in label and daily_delivery is None:
            daily_delivery = parse_aria_number(label, "权责外卖利润(万)")
        elif "团购利润(万)" in label and daily_group is None:
            daily_group = parse_aria_number(label, "团购利润(万)")
        elif "日期" in label and date_text is None and re.search(r"\d{1,2}/\d{1,2}/\d{4}", label):
            date_text = parse_aria_date(label)

    if date_text is None:
        match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", body_text)
        if not match:
            raise ValueError("未解析到利润数据日期。")
        date_text = parse_aria_date(match.group(1))

    if None in (daily_total, daily_delivery, daily_group):
        raise ValueError("未解析到当日利润 KPI。")

    lines = [line.strip() for line in body_text.splitlines()]
    month_total, month_delivery, month_group = parse_monthly_totals(lines)
    regions = parse_region_rows(lines)
    if not regions:
        raise ValueError("未解析到区域利润明细。")

    report_date = dt.datetime.strptime(date_text, "%Y-%m-%d")
    accounted_days = max(1, report_date.day)
    next_month = report_date.replace(day=28) + dt.timedelta(days=4)
    days_in_month = (next_month - dt.timedelta(days=next_month.day)).day
    forecast_total = month_total / accounted_days * days_in_month
    forecast_delivery = month_delivery / accounted_days * days_in_month
    forecast_group = month_group / accounted_days * days_in_month

    payload: list[dict[str, Any]] = [
        {
            "类型": "公司",
            "日期": date_text,
            "当日总利润": daily_total,
            "当日外卖利润": daily_delivery,
            "当日团购利润": daily_group,
            "月累计总利润": month_total,
            "月累计外卖利润": month_delivery,
            "月累计团购利润": month_group,
            "本月预计总利润": round(forecast_total, 2),
            "本月预计外卖利润": round(forecast_delivery, 2),
            "本月预计团购利润": round(forecast_group, 2),
        }
    ]
    payload.extend(regions)
    return payload

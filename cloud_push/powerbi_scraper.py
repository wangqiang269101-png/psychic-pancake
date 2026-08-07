"""Scrape daily profit payload from the embedded Power BI report."""

from __future__ import annotations

import base64
import datetime as dt
import json
import math
import os
import re
from typing import Any
from urllib.parse import urlparse

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


def load_browser_cookies() -> list[dict[str, Any]]:
    try:
        import browser_cookie3
    except ImportError as exc:  # noqa: BLE001
        raise RuntimeError("缺少 browser-cookie3，请安装 cloud_push/requirements.txt。") from exc

    cookies: list[dict[str, Any]] = []
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
                    "secure": bool(cookie.secure),
                    "httpOnly": bool(cookie.has_nonstandard_attr("HttpOnly")),
                }
            )
    if not cookies:
        raise RuntimeError("未从 Chrome 读取到 Power BI 登录 Cookie，请先在 Chrome 登录 Power BI。")
    return cookies


def load_storage_state() -> dict[str, Any]:
    """Load cloud auth from an encrypted secret, or safely extract local Chrome cookies."""
    encoded = os.getenv("POWERBI_STORAGE_STATE_B64", "").strip()
    if encoded:
        try:
            state = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("POWERBI_STORAGE_STATE_B64 不是有效的 base64 Playwright storage state。") from exc
        if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
            raise RuntimeError("Power BI storage state 缺少 cookies 数组。")
        if not state["cookies"]:
            raise RuntimeError("Power BI storage state 中没有 Cookie。")
        return state
    if os.getenv("GITHUB_ACTIONS", "").lower() == "true":
        raise RuntimeError(
            "Power BI 云端认证缺失：POWERBI_STORAGE_STATE_B64 未配置，"
            "GitHub runner 不允许回退到本机 Chrome profile。"
        )
    return {"cookies": load_browser_cookies(), "origins": []}


def _is_auth_endpoint(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in ("powerbi.com", "microsoftonline.com", "analysis.windows.net")
    )


def _storage_state_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return json.dumps(before, sort_keys=True, separators=(",", ":")) != json.dumps(
        after,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_aria_number(label: str, marker: str) -> float:
    match = re.search(rf"{re.escape(marker)}\s*[:：]?\s*(-?\d+(?:\.\d+)?)", label)
    if not match:
        raise ValueError(f"无法从 aria-label 解析 {marker}: {label}")
    return float(match.group(1))


def parse_aria_text(label: str, marker: str, following_markers: tuple[str, ...]) -> str | None:
    following = "|".join(re.escape(item) for item in following_markers)
    match = re.search(
        rf"{re.escape(marker)}\s*[:：]?\s*(.+?)(?=\s*(?:{following})\s*[:：]?|[,，;；]|$)",
        label,
    )
    if not match:
        return None
    return match.group(1).strip()


def parse_city_aria_rows(labels: list[str]) -> list[dict[str, Any]]:
    """Parse city table rows when Power BI exposes them as one aria label per row."""
    cities: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    city_markers = ("外卖城市", "城市名称", "城市")
    total_markers = ("权责总利润(万)", "当日总利润", "权责总利润")
    delivery_markers = ("权责外卖利润(万)", "当日外卖利润", "权责外卖利润")
    group_markers = ("团购利润(万)", "当日团购利润", "团购利润")
    all_following = ("区域名称", "区域", *total_markers, *delivery_markers, *group_markers)

    for label in labels:
        city_marker = next((marker for marker in city_markers if marker in label), None)
        total_marker = next((marker for marker in total_markers if marker in label), None)
        delivery_marker = next((marker for marker in delivery_markers if marker in label), None)
        group_marker = next((marker for marker in group_markers if marker in label), None)
        if not all((city_marker, total_marker, delivery_marker, group_marker)):
            continue
        city_name = parse_aria_text(label, city_marker, all_following)
        if not city_name or city_name in {"城市", "城市名称", "外卖城市", "Total"}:
            continue
        region_marker = next((marker for marker in ("区域名称", "区域") if marker in label), None)
        region_name = (
            parse_aria_text(label, region_marker, (*total_markers, *delivery_markers, *group_markers))
            if region_marker
            else ""
        )
        key = (city_name, region_name or "")
        if key in seen:
            continue
        seen.add(key)
        cities.append(
            {
                "类型": "城市",
                "城市": city_name,
                "区域": region_name or "",
                "当日总利润": parse_aria_number(label, total_marker),
                "当日外卖利润": parse_aria_number(label, delivery_marker),
                "当日团购利润": parse_aria_number(label, group_marker),
            }
        )
    return cities


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


def _semantic_command(query_payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return query_payload["queries"][0]["Query"]["Commands"][0]["SemanticQueryDataShapeCommand"]
    except (KeyError, IndexError, TypeError):
        return None


def _projection_index(command: dict[str, Any], native_name: str) -> int | None:
    for index, item in enumerate(command.get("Query", {}).get("Select", [])):
        if str(item.get("NativeReferenceName", "")).strip() == native_name:
            return index
    return None


def is_city_matrix_query(query_payload: dict[str, Any]) -> bool:
    command = _semantic_command(query_payload)
    if not command:
        return False
    required = ("区域", "城市", "权责总利润(万)")
    return all(_projection_index(command, name) is not None for name in required)


def expand_city_matrix_query(query_payload: dict[str, Any]) -> dict[str, Any]:
    """Enable both region and city projections in the captured matrix query."""
    expanded = json.loads(json.dumps(query_payload, ensure_ascii=False))
    command = _semantic_command(expanded)
    if not command:
        raise ValueError("Power BI 城市矩阵 query 缺少 SemanticQueryDataShapeCommand。")

    region_index = _projection_index(command, "区域")
    city_index = _projection_index(command, "城市")
    if region_index is None or city_index is None:
        raise ValueError("Power BI 城市矩阵 query 缺少区域/城市层级。")

    groupings = command.get("Binding", {}).get("Primary", {}).get("Groupings", [])
    if not groupings:
        raise ValueError("Power BI 城市矩阵 query 缺少 Primary Groupings。")
    projections = groupings[0].setdefault("Projections", [])
    if city_index not in projections:
        insert_at = projections.index(region_index) + 1 if region_index in projections else 0
        projections.insert(insert_at, city_index)

    query = expanded["queries"][0]
    query["CacheKey"] = json.dumps(
        {"Commands": query["Query"]["Commands"]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return expanded


def _decode_dsr_rows(ds: dict[str, Any], member: str = "DM1") -> list[dict[str, Any]]:
    """Decode Power BI's dictionary and bitmask-compressed matrix rows."""
    raw_rows: list[dict[str, Any]] = []
    for phase in ds.get("PH", []):
        raw_rows.extend(phase.get(member, []))
    if not raw_rows:
        return []

    dictionaries = ds.get("ValueDicts", {})
    schema: list[dict[str, Any]] = []
    previous: list[Any] = []
    decoded: list[dict[str, Any]] = []

    for raw in raw_rows:
        if raw.get("S"):
            schema = raw["S"]
        if not schema:
            continue
        repeated = int(raw.get("R", 0))
        nulls = int(raw.get("Ø", 0))
        supplied = iter(raw.get("C", []))
        values: list[Any] = []
        for index, field in enumerate(schema):
            if repeated & (1 << index):
                value = previous[index]
            elif nulls & (1 << index):
                value = None
            else:
                value = next(supplied, None)
            dictionary_name = field.get("DN")
            if dictionary_name and isinstance(value, int):
                dictionary = dictionaries.get(dictionary_name, [])
                if 0 <= value < len(dictionary):
                    value = dictionary[value]
            values.append(value)
        previous = values
        decoded.append({field["N"]: values[index] for index, field in enumerate(schema)})
    return decoded


def parse_city_matrix_rows(
    query_payload: dict[str, Any],
    query_response: dict[str, Any],
) -> list[dict[str, Any]]:
    command = _semantic_command(query_payload)
    if not command:
        return []
    try:
        data = query_response["results"][0]["result"]["data"]
        descriptor = data["descriptor"]["Select"]
        ds = data["dsr"]["DS"][0]
    except (KeyError, IndexError, TypeError):
        return []

    value_names: dict[str, str] = {}
    selects = command.get("Query", {}).get("Select", [])
    for index, semantic_select in enumerate(selects):
        if index >= len(descriptor) or not descriptor[index]:
            continue
        native_name = str(semantic_select.get("NativeReferenceName", "")).strip()
        value_name = descriptor[index].get("Value")
        if native_name and value_name:
            value_names[native_name] = value_name

    required = ("区域", "城市", "权责总利润(万)")
    if not all(name in value_names for name in required):
        return []

    rows: list[dict[str, Any]] = []
    for decoded in _decode_dsr_rows(ds):
        region = decoded.get(value_names["区域"])
        city = decoded.get(value_names["城市"])
        total = decoded.get(value_names["权责总利润(万)"])
        if not region or not city or total is None:
            continue
        try:
            total_number = float(total)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(total_number):
            continue

        def optional_number(name: str) -> float | None:
            value = decoded.get(value_names.get(name, ""))
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        rows.append(
            {
                "类型": "城市",
                "区域": str(region),
                "城市": str(city),
                "当日总利润": total_number,
                "当日外卖利润": optional_number("权责外卖利润(万)"),
                "当日团购利润": optional_number("团购利润(万)"),
            }
        )
    return rows


def fetch_expanded_city_rows(
    context: Any,
    captured_queries: list[tuple[Any, dict[str, Any], dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Parse existing child rows, otherwise replay the visual query with city enabled."""
    for _, query_payload, query_response in reversed(captured_queries):
        rows = parse_city_matrix_rows(query_payload, query_response)
        if rows:
            return rows

    if not captured_queries:
        raise ValueError("未捕获到包含区域→城市层级的 Power BI visual query。")
    request, query_payload, _ = captured_queries[-1]
    expanded = expand_city_matrix_query(query_payload)
    headers = {
        key: value
        for key, value in request.all_headers().items()
        if not key.startswith(":")
        and key.lower() not in {"content-length", "host", "origin", "referer"}
    }
    response = context.request.post(
        request.url,
        headers=headers,
        data=expanded,
        timeout=120000,
    )
    if response.status != 200:
        raise RuntimeError(f"Power BI 城市展开 query 失败: HTTP {response.status} {response.text()[:300]}")
    rows = parse_city_matrix_rows(expanded, response.json())
    if not rows:
        raise ValueError("Power BI 城市展开 query 成功，但未解析到城市明细。")
    return rows


def scrape_powerbi_payload(report_url: str) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # noqa: BLE001
        raise RuntimeError("缺少 playwright，请安装 cloud_push/requirements.txt。") from exc

    storage_state = load_storage_state()
    aria_labels: list[str] = []
    body_text = ""
    captured_city_queries: list[tuple[Any, dict[str, Any], dict[str, Any]]] = []
    cities: list[dict[str, Any]] = []
    auth_failure_statuses: set[int] = set()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            viewport={"width": 1920, "height": 1400},
            storage_state=storage_state,
        )
        try:
            page = context.new_page()

            def capture_query_response(response: Any) -> None:
                if (
                    response.status in (401, 403)
                    and "/public/query" in response.url
                    and _is_auth_endpoint(response.url)
                ):
                    auth_failure_statuses.add(response.status)
                if "/public/query" not in response.url:
                    return
                try:
                    query_payload = json.loads(response.request.post_data or "{}")
                    if is_city_matrix_query(query_payload):
                        captured_city_queries.append((response.request, query_payload, response.json()))
                except Exception:
                    return

            page.on("response", capture_query_response)
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
            current_host = (urlparse(page.url).hostname or "").lower()
            if auth_failure_statuses:
                statuses = ",".join(str(status) for status in sorted(auth_failure_statuses))
                raise RuntimeError(
                    f"Power BI 认证已失效（HTTP {statuses}）；请安全更新 POWERBI_STORAGE_STATE_B64。"
                )
            if current_host.endswith("microsoftonline.com") or (
                not captured_city_queries
                and any(marker in body_text.lower() for marker in ("sign in", "登录", "登入"))
            ):
                raise RuntimeError(
                    "Power BI 认证已失效或触发条件访问登录；请安全更新 POWERBI_STORAGE_STATE_B64。"
                )
            for element in page.locator("[aria-label]").all():
                try:
                    label = element.get_attribute("aria-label")
                    if label:
                        aria_labels.append(label)
                except Exception:
                    continue
            try:
                cities = fetch_expanded_city_rows(context, captured_city_queries)
            except Exception as city_exc:  # noqa: BLE001
                # Cloud runners often cannot expand city hierarchy; region+KPI still valid.
                print(f"[powerbi_scraper] city expand skipped: {city_exc}")
                cities = []
            refreshed_state = context.storage_state()
            os.environ["POWERBI_AUTH_STATE_CHANGED"] = str(
                _storage_state_changed(storage_state, refreshed_state)
            ).lower()
            os.environ["POWERBI_AUTH_ORIGIN_STORAGE_PRESENT"] = str(
                bool(storage_state.get("origins"))
            ).lower()
        finally:
            context.close()
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
    payload.extend(cities)
    return payload

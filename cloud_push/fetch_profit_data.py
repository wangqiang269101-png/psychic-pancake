#!/usr/bin/env python3
"""Fetch latest profit payload from Power BI and save as JSON."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from powerbi_scraper import scrape_powerbi_payload  # noqa: E402


DEFAULT_POWERBI_URL = (
    "https://app.powerbi.com/reportEmbed?"
    "reportId=aa5bfe1a-b218-4be5-83e3-ce61bfd8d6c2&autoAuth=true&ctid=7c792a97-2300-4444-aa97-172fed9b0501"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch latest profit payload JSON")
    parser.add_argument("output", nargs="?", help="output JSON path")
    parser.add_argument("--report-url", default=os.getenv("POWERBI_REPORT_URL", DEFAULT_POWERBI_URL))
    args = parser.parse_args()

    payload = scrape_powerbi_payload(args.report_url)
    company = next(row for row in payload if str(row.get("类型", "")).strip() == "公司")
    output = Path(args.output) if args.output else ROOT / "outputs" / f"latest-profit-{company['日期']}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "date": company["日期"], "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

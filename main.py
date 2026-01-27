import csv
import calendar
import datetime as dt
import re
from pathlib import Path
from typing import List, Dict, Tuple, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# =========================
# CONFIG (edit these)
# =========================
YEAR = 2026
MONTH = 1

# Location/station portion of the URL you showed:
BASE_URL = "https://www.wunderground.com/history/daily/us/oh/new-knoxville/KAXV/date"

OUTPUT_CSV = Path(f"wu_daily_observations_{YEAR}_{MONTH:02d}.csv")

HEADLESS = True
NAV_TIMEOUT_MS = 45_000
TABLE_TIMEOUT_MS = 30_000


def month_dates(year: int, month: int) -> List[dt.date]:
    last_day = calendar.monthrange(year, month)[1]
    return [dt.date(year, month, day) for day in range(1, last_day + 1)]


def build_url(base_url: str, date_obj: dt.date) -> str:
    # WU accepts non-zero-padded month/day like 2026-1-7, matching your example
    return f"{base_url}/{date_obj.year}-{date_obj.month}-{date_obj.day}"


JS_EXTRACT_DAILY_OBS_TABLE = r"""
() => {
  const table = document.querySelector(".observation-table table");
  if (!table) {
    return { ok: false, reason: "Daily Observations table not found" };
  }

  if (!table) return { ok: false, reason: "No table found" };

  const ths = Array.from(table.querySelectorAll("thead th")).map(th => (th.textContent || "").trim());
  const headers = ths.length ? ths : Array.from(table.querySelectorAll("tr th")).map(th => (th.textContent || "").trim());

  const rows = Array.from(table.querySelectorAll("tbody tr")).map(tr => {
    const tds = Array.from(tr.querySelectorAll("td")).map(td => (td.textContent || "").replace(/\s+/g, " ").trim());
    return tds;
  }).filter(r => r.length > 0);

  return { ok: true, headers, rows };
}
"""


def scrape_one_day(page, date_obj: dt.date) -> Tuple[List[str], List[List[str]]]:
    url = build_url(BASE_URL, date_obj)
    page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

    # Wait for the section/table to appear.
    # We wait on the presence of some "Daily Observations" text OR any table.
    try:
        page.wait_for_selector(".observation-table table", timeout=TABLE_TIMEOUT_MS)
    except PlaywrightTimeoutError as e:
        raise RuntimeError(f"Timed out waiting for Daily Observations table on {url}") from e

    data = page.evaluate(JS_EXTRACT_DAILY_OBS_TABLE)
    if not data.get("ok"):
        raise RuntimeError(f"Could not extract table on {url}: {data.get('reason')}")

    headers: List[str] = data.get("headers") or []
    rows: List[List[str]] = data.get("rows") or []

    # Some WU tables might include an empty first header or weird whitespace
    headers = [h.strip() for h in headers if h is not None]

    # If headers look missing, infer width from first row
    if (not headers) and rows:
        headers = [f"col_{i+1}" for i in range(len(rows[0]))]

    # Ensure each row has same length as headers (pad/truncate)
    if rows and headers:
        w = len(headers)
        fixed = []
        for r in rows:
            if len(r) < w:
                fixed.append(r + [""] * (w - len(r)))
            else:
                fixed.append(r[:w])
        rows = fixed

    return headers, rows


def write_csv(output_path: Path, all_rows: List[Dict[str, str]], fieldnames: List[str]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)


def should_attempt_date(d: dt.date) -> bool:
    today = dt.date.today()
    # Never attempt future dates
    return d <= today

NUM_UNIT_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*(.*)\s*$")

def split_number_and_unit(s: str) -> tuple[str, str]:
    """
    Returns (number_string, unit_string). If it doesn't look numeric, returns ("", "").
    """
    if s is None:
        return "", ""
    s = s.strip()
    if not s:
        return "", ""

    m = NUM_UNIT_RE.match(s)
    if not m:
        return "", ""

    num, unit = m.group(1), m.group(2)

    # If the "number" isn't actually numeric, bail
    try:
        float(num)
    except ValueError:
        return "", ""

    # Normalize common Weather Underground unit spellings
    unit = unit.replace("Â", "").strip()

    # Normalize known units
    unit = unit.replace("°F", "°F").replace("°C", "°C")

    # Remove degree symbol from non-temperature units
    if unit not in {"°F", "°C"}:
        unit = unit.replace("°", "")

    # Normalize spacing
    unit = unit.replace(" %", "%").replace("% ", "%")
    unit = unit.replace(" mph", "mph").replace(" in", "in")

    return num, unit


def infer_units_from_rows(headers: list[str], rows: list[list[str]]) -> dict[str, str]:
    """
    Look through rows and infer a unit per header (best-effort).
    Only assigns units where values appear numeric-with-unit.
    """
    units: dict[str, str] = {}

    # Scan a limited number of rows for speed; increase if needed
    scan_limit = min(len(rows), 30)

    for col_idx, h in enumerate(headers):
        # Skip columns we never want to unit-strip
        if h.lower() in {"time", "wind", "condition"}:
            continue

        # Find first non-empty numeric cell and grab its unit
        for r in rows[:scan_limit]:
            if col_idx >= len(r):
                continue
            num, unit = split_number_and_unit(r[col_idx])
            if num and unit:
                units[h] = unit
                break

    return units


def strip_units_in_rows(headers: list[str], rows: list[list[str]], units: dict[str, str]) -> list[list[str]]:
    """
    For columns with inferred units, replace cell value with the numeric portion only.
    """
    out_rows: list[list[str]] = []

    for r in rows:
        new_r = r[:]  # copy
        for col_idx, h in enumerate(headers):
            if h not in units:
                continue
            if col_idx >= len(new_r):
                continue
            num, unit = split_number_and_unit(new_r[col_idx])
            # Only strip if it matches the unit we inferred for that column
            if num and unit == units[h]:
                new_r[col_idx] = num
        out_rows.append(new_r)

    return out_rows


def apply_units_to_headers(headers: list[str], units: dict[str, str]) -> list[str]:
    """
    Returns headers where unit-bearing columns become like 'Temperature (°F)'.
    """
    new_headers = []
    for h in headers:
        if h in units:
            new_headers.append(f"{h} ({units[h]})")
        else:
            new_headers.append(h)
    return new_headers



def main() -> None:
    dates = month_dates(YEAR, MONTH)

    all_rows: List[Dict[str, str]] = []
    canonical_headers: Optional[List[str]] = None  # WU headers (without Date)

    today = dt.date.today()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        page = context.new_page()

        for d in dates:
            if not should_attempt_date(d):
                print(f"[SKIP] {d.isoformat()} is in the future (no data yet).")
                continue

            try:
                headers, rows = scrape_one_day(page, d)
                units = infer_units_from_rows(headers, rows)
                rows = strip_units_in_rows(headers, rows, units)
                headers_with_units = apply_units_to_headers(headers, units)

                # Catch: page exists but table has no rows yet
                if not rows:
                    # For today, this can happen early in the day; for past days, it's unusual but possible.
                    tag = "TODAY" if d == today else "PAST"
                    print(f"[NO DATA] {d.isoformat()} ({tag}) table has 0 rows. Skipping.")
                    continue

                # Establish canonical header set from first successful day
                if canonical_headers is None:
                    canonical_headers = headers_with_units

                header_out_name = {h: (f"{h} ({units[h]})" if h in units else h) for h in headers}

                # Map this day's rows into canonical schema by header name
                row_dicts = []
                for r in rows:
                    day_map = {headers[i]: r[i] for i in range(min(len(headers), len(r)))}
                    out = {"Date": d.isoformat()}
                    # day_map uses original headers; canonical_headers uses unitized header names
                    for orig_h in headers:
                        out_name = header_out_name[orig_h]
                        out[out_name] = day_map.get(orig_h, "")

                    row_dicts.append(out)

                all_rows.extend(row_dicts)

                # Report partial-day vs full-day (optional)
                if d == today:
                    print(f"[OK] {d.isoformat()} (today/partial) -> {len(rows)} rows")
                else:
                    print(f"[OK] {d.isoformat()} -> {len(rows)} rows")

            except Exception as e:
                # Catch: missing table, changed markup, or WU not showing data yet
                # If it's today or a future-ish date (but we already filtered future), treat as "no data"
                if d == today:
                    print(f"[NO DATA] {d.isoformat()} (today) page not ready / table missing. Skipping. ({e})")
                    continue

                # For past days, still skip but log as warning (you may want to investigate)
                print(f"[WARN] {d.isoformat()} failed: {e} (skipping)")

        browser.close()

    if not canonical_headers:
        raise SystemExit("No days were successfully scraped; nothing to write.")

    fieldnames = ["Date"] + canonical_headers
    write_csv(OUTPUT_CSV, all_rows, fieldnames)

    print(f"\nDone. Wrote {len(all_rows)} rows to: {OUTPUT_CSV.resolve()}")


if __name__ == "__main__":
    main()

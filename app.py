import re
import csv
import calendar
import datetime as dt
from pathlib import Path
from typing import List, Dict, Tuple, Optional

import gradio as gr
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# =========================
# CONFIG (edit these)
# =========================
LOCATION_URLS = {
    "G. A. Wintzer - Wapakoneta": "https://www.wunderground.com/history/daily/us/oh/new-knoxville/KAXV/date",
    "Bardot - Nazareth": "https://www.wunderground.com/history/daily/us/pa/allentown/KABE/date",
    "Ultra Poly - Portland": "https://www.wunderground.com/history/daily/us/nj/andover/K12N/date",
    "OMF & GMF - Ontario": "https://www.wunderground.com/history/daily/ca/oshawa/CYOO/date",
    "Club Coffee - Etobicoke": "https://www.wunderground.com/history/daily/ca/mississauga/CYYZ/date",
    "Bergen County Jail - Hackensack": "https://www.wunderground.com/history/daily/us/nj/teterboro/KTEB/date",
}
DEFAULT_LOCATION = "G. A. Wintzer - Wapakoneta"

HEADLESS = True
NAV_TIMEOUT_MS = 45_000
TABLE_TIMEOUT_MS = 30_000
HIDDEN_ALWAYS_INCLUDE = ["Time"]
DATE_COLUMN = "Date"

# Uses a BOM so Excel opens UTF-8 correctly (prevents Â° artifacts)
CSV_ENCODING = "utf-8-sig"

# =========================
# Utilities
# =========================
def month_dates(year: int, month: int) -> List[dt.date]:
    last_day = calendar.monthrange(year, month)[1]
    return [dt.date(year, month, day) for day in range(1, last_day + 1)]

def build_url(base_url: str, date_obj: dt.date) -> str:
    return f"{base_url}/{date_obj.year}-{date_obj.month}-{date_obj.day}"

# =========================
# In-page extractor JS
# =========================
JS_EXTRACT_DAILY_OBS_TABLE = r"""
() => {
  const table = document.querySelector(".observation-table table");
  if (!table) {
    return { ok: false, reason: "Daily Observations table not found" };
  }

  const ths = Array.from(table.querySelectorAll("thead th")).map(th => (th.textContent || "").trim());
  const headers = ths.length ? ths : Array.from(table.querySelectorAll("tr th")).map(th => (th.textContent || "").trim());

  const rows = Array.from(table.querySelectorAll("tbody tr")).map(tr => {
    const tds = Array.from(tr.querySelectorAll("td")).map(td => (td.textContent || "").replace(/\s+/g, " ").trim());
    return tds;
  }).filter(r => r.length > 0);

  return { ok: true, headers, rows };
}
"""

# =========================
# Unit stripping helpers
# =========================
NUM_UNIT_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*(.*)\s*$")

def split_number_and_unit(s: str) -> tuple[str, str]:
    """
    Returns (number_string, unit_string). If it doesn't look numeric, returns ("", "").
    Also normalizes Weather Underground's formatting where '°' is used as a separator.
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

    try:
        float(num)
    except ValueError:
        return "", ""

    unit = unit.replace("Â", "").strip()

    # WU often uses "°" as a separator for many units; keep it only for temperature units.
    # Examples: "10 °F" -> "°F", "73 °%" -> "%", "21 °mph" -> "mph", "29.02 °in" -> "in"
    # First, collapse spaces
    unit = unit.replace(" ", "")

    # Normalize true degree units
    if unit in {"°F", "°C"}:
        return num, unit

    # For everything else, strip degree symbol if present
    unit = unit.replace("°", "")

    # Normalize common units
    if unit == "%":
        return num, "%"
    if unit.lower() == "mph":
        return num, "mph"
    if unit.lower() == "in":
        return num, "in"

    return num, unit

def infer_units_from_rows(headers: list[str], rows: list[list[str]]) -> dict[str, str]:
    units: dict[str, str] = {}
    scan_limit = min(len(rows), 30)

    for col_idx, h in enumerate(headers):
        if h.lower() in {"time", "wind", "condition"}:
            continue

        for r in rows[:scan_limit]:
            if col_idx >= len(r):
                continue
            num, unit = split_number_and_unit(r[col_idx])
            if num and unit:
                units[h] = unit
                break

    return units

def strip_units_in_rows(headers: list[str], rows: list[list[str]], units: dict[str, str]) -> list[list[str]]:
    out_rows: list[list[str]] = []
    for r in rows:
        new_r = r[:]
        for col_idx, h in enumerate(headers):
            if h not in units:
                continue
            if col_idx >= len(new_r):
                continue
            num, unit = split_number_and_unit(new_r[col_idx])
            if num and unit == units[h]:
                new_r[col_idx] = num
        out_rows.append(new_r)
    return out_rows

def apply_units_to_headers(headers: list[str], units: dict[str, str]) -> list[str]:
    new_headers = []
    for h in headers:
        if h in units:
            new_headers.append(f"{h} ({units[h]})")
        else:
            new_headers.append(h)
    return new_headers

# =========================
# Scraping core
# =========================
def scrape_one_day(page, base_url: str, date_obj: dt.date) -> Tuple[List[str], List[List[str]]]:
    url = build_url(base_url, date_obj)
    page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

    try:
        page.wait_for_selector(".observation-table table", timeout=TABLE_TIMEOUT_MS)
    except PlaywrightTimeoutError as e:
        raise RuntimeError(f"Timed out waiting for Daily Observations table on {url}") from e

    data = page.evaluate(JS_EXTRACT_DAILY_OBS_TABLE)
    if not data.get("ok"):
        raise RuntimeError(f"Could not extract table on {url}: {data.get('reason')}")

    headers: List[str] = data.get("headers") or []
    rows: List[List[str]] = data.get("rows") or []

    headers = [h.strip() for h in headers if h is not None]

    if (not headers) and rows:
        headers = [f"col_{i+1}" for i in range(len(rows[0]))]

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
    with output_path.open("w", newline="", encoding=CSV_ENCODING) as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

def run_month_scrape(base_url: str, location_label: str, year: int, month: int, selected_columns: Optional[list[str]] = None) -> tuple[Optional[str], str]:

    """
    Returns (csv_path, log_text)
    """
    dates = month_dates(year, month)
    today = dt.date.today()

    # Timestamped output prevents Windows PermissionError if user has an older CSV open in Excel.
    safe_loc = re.sub(r"[^A-Za-z0-9]+", "_", location_label).strip("_")
    out_path = Path(f"trends_{safe_loc}_{year}_{month:02d}_{dt.datetime.now():%Y%m%d_%H%M%S}.csv")

    all_rows: List[Dict[str, str]] = []
    canonical_headers_out: Optional[List[str]] = None

    log_lines: List[str] = []
    log_lines.append(f"Scraping {location_label} — {year}-{month:02d}")
    log_lines.append(f"Base URL: {base_url}")
    log_lines.append(f"Today is {today.isoformat()}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        page = context.new_page()

        for d in dates:
            if d > today:
                log_lines.append(f"[SKIP] {d.isoformat()} is in the future (no data yet).")
                continue

            try:
                headers, rows = scrape_one_day(page, base_url, d)

                if not rows:
                    tag = "today/partial" if d == today else "past"
                    log_lines.append(f"[NO DATA] {d.isoformat()} ({tag}) table has 0 rows. Skipping.")
                    continue

                # Strip units into headers
                units = infer_units_from_rows(headers, rows)
                rows = strip_units_in_rows(headers, rows, units)
                headers_out = apply_units_to_headers(headers, units)
                header_out_name = {h: (f"{h} ({units[h]})" if h in units else h) for h in headers}

                if canonical_headers_out is None:
                    canonical_headers_out = headers_out

                # Build per-row dicts
                for r in rows:
                    day_map = {headers[i]: r[i] for i in range(min(len(headers), len(r)))}
                    out = {"Date": d.isoformat()}
                    # Write out in canonical (unitized) header names
                    for orig_h in headers:
                        out_name = header_out_name[orig_h]
                        out[out_name] = day_map.get(orig_h, "")
                    all_rows.append(out)

                if d == today:
                    log_lines.append(f"[OK] {d.isoformat()} (today/partial) -> {len(rows)} rows")
                else:
                    log_lines.append(f"[OK] {d.isoformat()} -> {len(rows)} rows")

            except Exception as e:
                if d == today:
                    log_lines.append(f"[NO DATA] {d.isoformat()} (today) not ready / table missing. Skipping. ({e})")
                else:
                    log_lines.append(f"[WARN] {d.isoformat()} failed: {e} (skipping)")

        browser.close()

    if not canonical_headers_out:
        return None, "\n".join(log_lines + ["\nNo days were successfully scraped; nothing to write."])

    # --- Column filtering ---
    # Date is always included.
    selected_set = set(selected_columns or [])
    # If user didn't pick anything, default to "all columns"
    if not selected_set:
        selected_headers = canonical_headers_out
    else:
        # Keep only headers that exist in the output
        selected_headers = [h for h in canonical_headers_out if h in selected_set]

    # Force-include hidden required columns (like Time) if they exist in output
    for required in HIDDEN_ALWAYS_INCLUDE:
        if required in canonical_headers_out and required not in selected_headers:
            selected_headers.insert(0, required)  # keep near the front

    fieldnames = [DATE_COLUMN] + selected_headers

    # Filter row dicts to only selected columns (+ Date)
    filtered_rows = []
    for r in all_rows:
        fr = {"Date": r.get("Date", "")}
        for h in selected_headers:
            fr[h] = r.get(h, "")
        filtered_rows.append(fr)

    write_csv(out_path, filtered_rows, fieldnames)

    log_lines.append(f"\nDone. Wrote {len(all_rows)} rows to: {out_path.resolve()}")
    return str(out_path), "\n".join(log_lines)

# =========================
# Gradio UI
# =========================

def gradio_load_columns(location_label: str, year: int, month: int):
    # Pick the first day in the month that is not in the future (prefer today or earlier)
    today = dt.date.today()
    dates = month_dates(year, month)
    probe = None
    base_url = LOCATION_URLS[location_label]
    for d in dates:
        if d <= today:
            probe = d
            break

    if probe is None:
        return gr.update(choices=[], value=[]), "All days in that month are in the future. No columns to load yet."

    log_lines = [f"Loading columns for {location_label} from {probe.isoformat()} ..."]

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=HEADLESS)
            context = browser.new_context()
            page = context.new_page()

            headers, rows = scrape_one_day(page, base_url, probe)
            if not rows:
                browser.close()
                return gr.update(choices=[], value=[]), f"No rows available on {probe.isoformat()} yet; cannot infer columns."

            units = infer_units_from_rows(headers, rows)
            headers_out = apply_units_to_headers(headers, units)
            ui_headers = [h for h in headers_out if h not in HIDDEN_ALWAYS_INCLUDE]

            browser.close()

        # Show unitized headers as choices; default select all of them
        return gr.update(choices=headers_out, value=headers_out), "\n".join(log_lines + ["Columns loaded."])
    except Exception as e:
        return gr.update(choices=[], value=[]), "\n".join(log_lines + [f"Failed to load columns: {e}"])

def gradio_run(location_label: str, year: int, month: int, selected_columns: list[str]):
    base_url = LOCATION_URLS[location_label]
    csv_path, log_text = run_month_scrape(base_url, location_label, year, month, selected_columns)
    if not csv_path:
        return None, log_text
    return csv_path, log_text

def build_app():
    current_year = dt.date.today().year
    years = list(range(current_year - 5, current_year + 2))
    months = list(range(1, 13))

    with gr.Blocks(title="Weather Underground Monthly Scraper") as demo:
        gr.Markdown("## WUGscraper: Weather Underground Daily Trends → Monthly CSV\nSelect a year/month, run, then download the CSV.")

        location_in = gr.Dropdown(
            choices=list(LOCATION_URLS.keys()),
            value=DEFAULT_LOCATION,
            label="Location"
        )

        with gr.Row():
            year_in = gr.Dropdown(choices=years, value=dt.date.today().year, label="Year")
            month_in = gr.Dropdown(choices=months, value=dt.date.today().month, label="Month")

        with gr.Row():
            load_cols_btn = gr.Button("Load available columns")
            cols_in = gr.CheckboxGroup(choices=[], value=[], label="Columns to include (Date always included)")

        run_btn = gr.Button("Run scrape")

        file_out = gr.File(label="Download CSV")
        log_out = gr.Textbox(label="Log", lines=18)

        load_cols_btn.click(
            fn=gradio_load_columns,
            inputs=[location_in, year_in, month_in],
            outputs=[cols_in, log_out],
        )

        run_btn.click(
            fn=gradio_run,
            inputs=[location_in, year_in, month_in, cols_in],
            outputs=[file_out, log_out],
        )

        gr.Markdown(
            "**Notes**\n"
            "- Future dates are skipped automatically.\n"
            "- Today may be partial.\n"
            "- Units are moved into headers; cells contain numbers only.\n"
            "- Output is UTF-8 with BOM for Excel compatibility.\n"
        )

    return demo

if __name__ == "__main__":
    app = build_app()
    app.launch()

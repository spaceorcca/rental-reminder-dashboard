"""
Rental Renewal Reminder Dashboard Generator
--------------------------------------------
Reads the Google Sheet of rental agreements, finds owners due a reminder
today (30 days before end date, or on the end date), and generates a
static HTML dashboard with one-tap "Send WhatsApp" buttons using wa.me
deep links. The actual send is always a manual tap by a human (you),
so this never risks WhatsApp ban/automation detection.

Owners with more than one property due on the same day are grouped into
a single card with one button per property, so a repeat owner doesn't
show up as several duplicate-looking rows.

Also surfaces a real "upcoming renewals" table (next 60 days) so the
page is never a blank screen even when nothing is due today, and shows
a genuine last-successful-sync timestamp rather than a fabricated
uptime statistic.

The generated page is published via GitHub Pages.

Required environment variables:
    GOOGLE_SERVICE_ACCOUNT_JSON  - full contents of the service account JSON key
    SPREADSHEET_ID               - the Google Sheet ID (the long string in its URL
                                    between /d/ and /edit)
"""

import os
import json
import html
from collections import defaultdict
from datetime import datetime, date
from urllib.parse import quote

import gspread
from google.oauth2.service_account import Credentials

LOG_TAB_NAME = "Log"
OUTPUT_DIR = "docs"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "index.html")
UPCOMING_WINDOW_DAYS = 60
UPCOMING_PREVIEW_LIMIT = 10

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")


def fail_fast_on_missing_env():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise SystemExit(
            "Missing required environment variable: GOOGLE_SERVICE_ACCOUNT_JSON. "
            "Set this as a GitHub Actions secret before running."
        )
    if not SPREADSHEET_ID:
        raise SystemExit(
            "Missing required environment variable: SPREADSHEET_ID. "
            "Set this as a GitHub Actions secret before running."
        )


def get_sheet_client():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    return gspread.authorize(creds)


def get_or_create_log_tab(spreadsheet):
    try:
        return spreadsheet.worksheet(LOG_TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=LOG_TAB_NAME, rows=1000, cols=6)
        ws.append_row(["Timestamp", "Owner", "Phone", "Flat", "Status", "Details"])
        return ws


def log_row(log_ws, owner, phone, flat, status, details):
    log_ws.append_row(
        [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), owner, phone, flat, status, details]
    )


def normalize_phone(raw_phone):
    """
    Strips formatting and ensures an Indian country code prefix.
    Assumes all numbers in the sheet are Indian mobiles. If you ever add a
    non-Indian owner's number, type it with its country code already
    included in the sheet (e.g. "44..." for UK) so this function leaves it alone.
    """
    digits = "".join(ch for ch in str(raw_phone) if ch.isdigit())
    if len(digits) == 10:
        return f"91{digits}"
    return digits


def build_message(owner, flat, tenant, end_date_display):
    tenant_text = f" (occupied by *{tenant}*)" if tenant else ""
    return (
        f"✦ Hi {owner}!\n\n"
        f"Your rental agreement for ✦ *{flat}*{tenant_text} will end on ✦ *{end_date_display}*.\n"
        f"Let's make sure everything stays smooth and stress-free!\n\n"
        f"✦ Why renewing is the best choice:\n"
        f"➤ Avoid last-minute hassle\n"
        f"➤ Keep your property secure\n"
        f"➤ Enjoy uninterrupted rental income\n"
        f"➤ Ensure peace of mind with a confirmed extension\n\n"
        f"We truly value this great partnership and want to keep things simple and beneficial for you.\n\n"
        f"Looking forward to continuing this positive experience!\n\n"
        f"✦ Cheers,\n"
        f"✦ Talib\n"
        f"✦ Contact: 7020204238"
    )


def wa_link(phone, message):
    return f"https://wa.me/{phone}?text={quote(message)}"


# -------------------------------------------------------------------------
# HTML template — clean, flat, editorial style. No blur/gradients/webfonts,
# so it renders identically and legibly on any phone in any lighting.
# -------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Rental Renewal Reminders — {generated_date}</title>
<style>
  :root {{
    --bg: #fafaf9;
    --surface: #ffffff;
    --border: #e7e5e4;
    --text: #1c1917;
    --text-dim: #78716c;
    --accent: #16a34a;
    --danger: #dc2626;
    --danger-bg: #fef2f2;
    --amber-bg: #fffbeb;
    --amber-text: #92400e;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: 28px 18px 50px;
    -webkit-font-smoothing: antialiased;
  }}
  .container {{ max-width: 620px; margin: 0 auto; }}

  .top {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 4px; flex-wrap: wrap; gap: 6px; }}
  .top h1 {{ font-size: 19px; font-weight: 600; letter-spacing: -0.2px; }}
  .top .date {{ font-size: 12.5px; color: var(--text-dim); }}
  .sync-line {{ font-size: 12px; color: var(--text-dim); margin-bottom: 22px; }}

  .stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 24px; }}
  .stat {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }}
  .stat .label {{ font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.4px; font-weight: 600; }}
  .stat .value {{ font-size: 22px; font-weight: 700; margin-top: 3px; }}
  .stat.warn .value {{ color: var(--danger); }}
  .stat.ok .value {{ color: var(--accent); }}

  h2 {{
    font-size: 13px; font-weight: 600; color: var(--text-dim); text-transform: uppercase;
    letter-spacing: 0.4px; margin: 26px 0 10px; display: flex; justify-content: space-between; align-items: baseline;
  }}
  h2 .count {{ font-size: 11.5px; font-weight: 600; color: var(--text-dim); text-transform: none; letter-spacing: 0; }}

  /* Owner group card — one per owner, holds 1+ property rows */
  .owner-card {{
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 14px 16px; margin-bottom: 8px;
  }}
  .owner-head {{
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    margin-bottom: 4px; flex-wrap: wrap;
  }}
  .owner-name {{ font-size: 14.5px; font-weight: 600; }}
  .owner-count {{ font-size: 11.5px; color: var(--text-dim); font-weight: 500; }}

  .property-row {{
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding: 9px 0; border-top: 1px solid var(--border); flex-wrap: wrap;
  }}
  .owner-card .property-row:first-of-type {{ border-top: none; padding-top: 6px; }}
  .property-main {{ min-width: 0; }}
  .property-top {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .flat-name {{ font-size: 13.5px; font-weight: 600; }}
  .pill {{
    font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 5px;
    text-transform: uppercase; letter-spacing: 0.2px; white-space: nowrap;
  }}
  .pill.today {{ background: var(--danger-bg); color: var(--danger); }}
  .pill.soon {{ background: var(--amber-bg); color: var(--amber-text); }}
  .sub {{ font-size: 12px; color: var(--text-dim); margin-top: 1px; }}
  .send-btn {{
    flex-shrink: 0; background: var(--accent); color: white; font-weight: 600; font-size: 13px;
    text-decoration: none; padding: 8px 16px; border-radius: 8px;
  }}

  table {{ width: 100%; border-collapse: collapse; background: var(--surface); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }}
  th {{ text-align: left; font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.3px; font-weight: 600; padding: 10px 12px; border-bottom: 1px solid var(--border); }}
  td {{ padding: 10px 12px; font-size: 13.5px; border-bottom: 1px solid var(--border); }}
  tr:last-child td {{ border-bottom: none; }}
  .muted {{ color: var(--text-dim); }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }}
  th:last-child {{ text-align: right; }}

  .empty {{ font-size: 13px; color: var(--text-dim); background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }}

  footer {{ margin-top: 26px; font-size: 11.5px; color: var(--text-dim); line-height: 1.6; }}
</style>
</head>
<body>
  <div class="container">
    <div class="top">
      <h1>Rental renewal reminders</h1>
      <span class="date">{generated_date}</span>
    </div>
    <div class="sync-line">Last sync: {last_sync_display}</div>

    <div class="stats">
      <div class="stat">
        <div class="label">Tracked</div>
        <div class="value">{total_count}</div>
      </div>
      <div class="stat {due_stat_class}">
        <div class="label">Due today</div>
        <div class="value">{due_count}</div>
      </div>
      <div class="stat">
        <div class="label">Next {upcoming_window} days</div>
        <div class="value">{upcoming_count}</div>
      </div>
    </div>

    <h2>Action queue <span class="count">{owner_group_count}</span></h2>
    {due_section}

    <h2>Upcoming renewals</h2>
    <table>
      <tr><th>Property</th><th>Owner</th><th>Ends</th><th>Days left</th></tr>
      {upcoming_section}
    </table>

    <footer>
      Tap "Send" to open a pre-filled WhatsApp message from your own number.
      Nothing sends automatically — you review and tap Send yourself.
    </footer>
  </div>
</body>
</html>
"""

# One card per owner. Contains 1+ PROPERTY_ROW_TEMPLATE blocks inside it.
OWNER_CARD_TEMPLATE = """
        <div class="owner-card">
          <div class="owner-head">
            <span class="owner-name">{owner}</span>
            <span class="owner-count">{property_count}</span>
          </div>
          {property_rows}
        </div>
"""

PROPERTY_ROW_TEMPLATE = """
          <div class="property-row">
            <div class="property-main">
              <div class="property-top">
                <span class="flat-name">{flat}</span>
                <span class="pill {pill_class}">{pill_text}</span>
              </div>
              <div class="sub">{tenant_line}ends {end_date_display}</div>
            </div>
            <a class="send-btn" href="{link}" target="_blank" rel="noopener">Send</a>
          </div>
"""

UPCOMING_TABLE_ROW_TEMPLATE = """
      <tr>
        <td>{flat}</td>
        <td class="muted">{owner}</td>
        <td class="muted">{end_date_display}</td>
        <td class="num">{days_left}d</td>
      </tr>
"""


def get_last_sync_display(log_ws):
    """
    Pulls the most recent timestamp already written to the Log tab, so the
    'last sync' line reflects a real prior successful run rather than a
    made-up figure. Falls back to 'first run' if the log is empty.
    """
    try:
        records = log_ws.get_all_records()
    except Exception:
        return "first run"
    if not records:
        return "first run"
    last_ts = str(records[-1].get("Timestamp", "")).strip()
    return last_ts or "first run"


def main():
    fail_fast_on_missing_env()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    client = get_sheet_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    sheet = spreadsheet.sheet1
    log_ws = get_or_create_log_tab(spreadsheet)

    # Capture last sync time BEFORE this run appends any new rows, so it
    # genuinely reflects the previous run rather than the one in progress.
    last_sync_display = get_last_sync_display(log_ws)

    data = sheet.get_all_records()
    cleaned_data = [{k.replace(":", "").strip(): v for k, v in row.items()} for row in data]

    today = date.today()
    # Group due properties by (owner name, phone) so the same person with
    # multiple flats gets one card instead of duplicate owner-name rows.
    due_by_owner = defaultdict(list)
    upcoming_rows = []
    total_count = 0
    due_count = 0

    for row in cleaned_data:
        owner = str(row.get("Owner Name", "")).strip()
        tenant = str(row.get("Tenant Name", "")).strip()
        flat = str(row.get("Flat Address", "")).strip()
        raw_phone = str(row.get("Phone Number", "")).strip()
        end_date_str = str(row.get("End Date", "")).strip()

        if not end_date_str or not raw_phone:
            log_row(log_ws, owner, raw_phone, flat, "SKIPPED", "Missing date or phone")
            continue

        try:
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        except ValueError:
            log_row(log_ws, owner, raw_phone, flat, "SKIPPED", f"Invalid date format: {end_date_str}")
            continue

        # Only count real, parseable agreement rows toward "Tracked".
        total_count += 1

        days_left = (end_date - today).days
        end_date_display = end_date.strftime("%d %b %Y")

        # --- Due today / 30-day reminder (unchanged trigger logic) ---
        if days_left in (30, 0):
            phone = normalize_phone(raw_phone)
            message = build_message(owner, flat, tenant, end_date_display)
            link = wa_link(phone, message)

            pill_class = "today" if days_left == 0 else "soon"
            pill_text = "Ends today" if days_left == 0 else "30 days left"
            tenant_line = f"Tenant: {html.escape(tenant)} &middot; " if tenant else ""

            property_row_html = PROPERTY_ROW_TEMPLATE.format(
                flat=html.escape(flat),
                pill_class=pill_class,
                pill_text=pill_text,
                tenant_line=tenant_line,
                end_date_display=end_date_display,
                link=link,
            )

            # Group key: owner name + phone, so two different owners who
            # happen to share a name (unlikely but possible) aren't merged.
            group_key = (owner, phone)
            due_by_owner[group_key].append(property_row_html)
            due_count += 1

            log_row(log_ws, owner, phone, flat, "LISTED", f"days_left={days_left}")

        # --- Upcoming pipeline: any agreement ending in the next N days ---
        # (Independent of the due-today/30-day trigger — this is purely
        # informational so the dashboard is never a blank page.)
        if 0 < days_left <= UPCOMING_WINDOW_DAYS:
            upcoming_rows.append(
                {
                    "days_left": days_left,
                    "html": UPCOMING_TABLE_ROW_TEMPLATE.format(
                        flat=html.escape(flat),
                        owner=html.escape(owner),
                        end_date_display=end_date_display,
                        days_left=days_left,
                    ),
                }
            )

    # Build owner group cards, largest property count first so the owner
    # with the most to handle today is immediately visible up top.
    owner_groups = sorted(due_by_owner.items(), key=lambda kv: -len(kv[1]))
    owner_cards_html = []
    for (owner, _phone), property_rows in owner_groups:
        property_count_label = (
            "1 property" if len(property_rows) == 1 else f"{len(property_rows)} properties"
        )
        owner_cards_html.append(
            OWNER_CARD_TEMPLATE.format(
                owner=html.escape(owner),
                property_count=property_count_label,
                property_rows="".join(property_rows),
            )
        )

    # Sort upcoming by soonest first, cap the preview list
    upcoming_rows.sort(key=lambda r: r["days_left"])
    upcoming_preview = upcoming_rows[:UPCOMING_PREVIEW_LIMIT]

    generated_date = today.strftime("%d %b %Y")

    due_section = (
        "".join(owner_cards_html)
        if owner_cards_html
        else '<div class="empty">Nothing due today.</div>'
    )

    if upcoming_preview:
        upcoming_section = "".join(r["html"] for r in upcoming_preview)
        if len(upcoming_rows) > UPCOMING_PREVIEW_LIMIT:
            remaining = len(upcoming_rows) - UPCOMING_PREVIEW_LIMIT
            upcoming_section += (
                f'<tr><td colspan="4" class="muted">'
                f'+ {remaining} more in the next {UPCOMING_WINDOW_DAYS} days '
                f'(see your sheet for the full list)</td></tr>'
            )
    else:
        upcoming_section = (
            f'<tr><td colspan="4" class="muted">'
            f'No renewals in the next {UPCOMING_WINDOW_DAYS} days.</td></tr>'
        )

    due_stat_class = "warn" if due_count else "ok"
    owner_group_count = (
        f"{len(owner_groups)} owner{'s' if len(owner_groups) != 1 else ''}"
        if owner_groups
        else ""
    )

    final_html = HTML_TEMPLATE.format(
        generated_date=generated_date,
        last_sync_display=html.escape(last_sync_display),
        total_count=total_count,
        due_count=due_count,
        due_stat_class=due_stat_class,
        upcoming_window=UPCOMING_WINDOW_DAYS,
        upcoming_count=len(upcoming_rows),
        owner_group_count=owner_group_count,
        due_section=due_section,
        upcoming_section=upcoming_section,
    )

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(final_html)

    print(
        f"Dashboard generated: {due_count} due today across {len(owner_groups)} owner(s), "
        f"{len(upcoming_rows)} upcoming in next {UPCOMING_WINDOW_DAYS} days, "
        f"{total_count} total tracked."
    )


if __name__ == "__main__":
    main()

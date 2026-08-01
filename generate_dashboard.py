"""
Rental Renewal Reminder Dashboard Generator
--------------------------------------------
Reads the Google Sheet of rental agreements and generates a static HTML
dashboard listing every agreement inside an active action window, with
one-tap "Send WhatsApp" buttons using wa.me deep links. The actual send
is always a manual tap by a human (you), so this never risks WhatsApp
ban/automation detection.

Filtering logic (window-based, not exact-day-match):
    -15 <= days_left <= 30   -> shown in the Action Queue, always, with a
                                 severity badge. This means missing a day
                                 checking the dashboard can never cause a
                                 reminder to be silently skipped — it just
                                 stays in the queue until it ages out past
                                 -15 days (at which point it's assumed to
                                 need a phone call, not a WhatsApp text).

Badge tiers:
    15 <= days_left <= 30   -> "30-Day Window"   (info blue)
    1  <= days_left <= 14   -> "Expiring Soon"   (warning orange)
    days_left == 0          -> "Expires Today"   (urgent red)
    -15 <= days_left < 0    -> "Overdue (Nd)"    (critical dark red)

Owners with more than one property inside the active window are grouped
into a single card with one property row per property, so a repeat
owner doesn't show up as several duplicate-looking cards.

A separate "Upcoming Renewals" table shows agreements further out
(31-60 days) purely for visibility — nothing to act on yet.

Design: dark, high-contrast theme, responsive (single column on phones,
multi-column on laptop/desktop). Three lightweight motion effects:
animated KPI count-up, staggered card entry on load, and a desktop-only
hover glow gated behind @media (hover: hover) so phones never load or
run that logic.

Known limitation: there is currently no "mark as renewed" mechanism, so
a renewed agreement will keep showing (with an escalating overdue badge)
until it naturally ages out past -15 days. This is accepted for now.

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

# Active window: overdue up to 15 days, out to 30 days before end date.
ACTIVE_WINDOW_MIN_DAYS = -15
ACTIVE_WINDOW_MAX_DAYS = 30

# "Upcoming" is purely informational: further out than the active window,
# up to this many days.
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


def classify_badge(days_left):
    """
    Returns (pill_css_class, pill_text, severity_rank) for a given days_left.
    severity_rank is used to sort the queue most-urgent-first (higher = more urgent).
    Only meaningful for rows already known to be inside the active window.
    """
    if days_left < 0:
        overdue_by = abs(days_left)
        return ("overdue", f"Overdue ({overdue_by}d)", 3 + overdue_by)
    if days_left == 0:
        return ("today", "Expires Today", 3)
    if days_left <= 14:
        return ("soon", "Expiring Soon", 2)
    return ("window30", "30-Day Window", 1)


# -------------------------------------------------------------------------
# HTML template — dark, high-contrast, responsive. Flat surfaces (no blur)
# for reliable legibility; system fonts only, no external CDN dependency;
# multi-column on wide screens, single column on phones. Includes three
# lightweight motion effects: KPI count-up, staggered card entry, and a
# desktop-only hover glow gated behind @media (hover: hover).
# -------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Rental Renewal Reminders — {generated_date}</title>
<style>
  :root {{
    --bg: #0a0e14;
    --surface: #131824;
    --surface-2: #1a2130;
    --border: #262e3d;
    --text: #edf0f5;
    --text-dim: #8b96a8;
    --accent: #22c55e;
    --accent-soft: rgba(34, 197, 94, 0.14);
    --primary: #6d8bff;
    --primary-soft: rgba(109, 139, 255, 0.14);
    --danger: #f87171;
    --danger-bg: rgba(248, 113, 113, 0.14);
    --amber: #fbbf24;
    --amber-bg: rgba(251, 191, 36, 0.14);
    --critical: #ff5c7a;
    --critical-bg: rgba(255, 92, 122, 0.18);
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    padding: 28px 18px 60px;
    -webkit-font-smoothing: antialiased;
  }}
  .page {{ max-width: 1180px; margin: 0 auto; }}

  .top {{
    display: flex; justify-content: space-between; align-items: flex-start;
    margin-bottom: 4px; flex-wrap: wrap; gap: 10px;
  }}
  .brand {{ display: flex; align-items: center; gap: 12px; }}
  .logo {{
    width: 38px; height: 38px; border-radius: 10px; flex-shrink: 0;
    background: linear-gradient(135deg, var(--primary), var(--accent));
    display: flex; align-items: center; justify-content: center;
    font-weight: 800; font-size: 15px; color: #06101f;
  }}
  .top h1 {{ font-size: 20px; font-weight: 700; letter-spacing: -0.2px; }}
  .top .date {{ font-size: 12.5px; color: var(--text-dim); margin-top: 1px; }}
  .sync-badge {{
    font-size: 11.5px; color: var(--accent); background: var(--accent-soft);
    border: 1px solid rgba(34, 197, 94, 0.3); padding: 6px 12px; border-radius: 999px;
    font-weight: 600; white-space: nowrap;
  }}

  .stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin: 22px 0 28px; }}
  .stat {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }}
  .stat .label {{ font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; font-weight: 700; }}
  .stat .value {{ font-size: 26px; font-weight: 800; margin-top: 5px; font-variant-numeric: tabular-nums; }}
  .stat.warn .value {{ color: var(--danger); }}
  .stat.ok .value {{ color: var(--accent); }}

  h2 {{
    font-size: 13px; font-weight: 700; color: var(--text-dim); text-transform: uppercase;
    letter-spacing: 0.6px; margin: 30px 0 12px; display: flex; justify-content: space-between; align-items: baseline;
  }}
  h2 .count {{ font-size: 11.5px; font-weight: 600; color: var(--text-dim); text-transform: none; letter-spacing: 0; }}

  .owner-grid {{ display: grid; grid-template-columns: 1fr; gap: 12px; }}
  .owner-card {{
    background: var(--surface); border: 1px solid var(--border); border-radius: 14px;
    padding: 18px; display: flex; flex-direction: column;
    opacity: 0; transform: translateY(10px);
    animation: card-in 0.45s ease forwards;
  }}
  @keyframes card-in {{
    to {{ opacity: 1; transform: translateY(0); }}
  }}
  @media (hover: hover) and (pointer: fine) {{
    .owner-card {{ transition: box-shadow 0.2s ease, border-color 0.2s ease; }}
    .owner-card:hover {{
      border-color: rgba(109, 139, 255, 0.4);
      box-shadow: 0 0 0 1px rgba(109, 139, 255, 0.15), 0 8px 24px rgba(109, 139, 255, 0.12);
    }}
  }}
  .owner-head {{
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    margin-bottom: 6px; flex-wrap: wrap;
  }}
  .owner-name {{ font-size: 15px; font-weight: 700; }}
  .owner-count {{ font-size: 11.5px; color: var(--text-dim); font-weight: 500; }}

  .property-row {{
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding: 11px 0; border-top: 1px solid var(--border);
  }}
  .owner-card .property-row:first-of-type {{ border-top: none; padding-top: 8px; }}
  .property-main {{ min-width: 0; flex: 1 1 auto; }}
  .property-top {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .flat-name {{ font-size: 13.5px; font-weight: 600; line-height: 1.35; }}
  .pill {{
    font-size: 10px; font-weight: 700; padding: 3px 9px; border-radius: 999px;
    text-transform: uppercase; letter-spacing: 0.3px; white-space: nowrap; flex-shrink: 0;
  }}
  .pill.window30 {{ background: var(--primary-soft); color: var(--primary); }}
  .pill.soon {{ background: var(--amber-bg); color: var(--amber); }}
  .pill.today {{ background: var(--danger-bg); color: var(--danger); }}
  .pill.overdue {{ background: var(--critical-bg); color: var(--critical); }}
  .sub {{ font-size: 12px; color: var(--text-dim); margin-top: 3px; }}
  .send-btn {{
    flex-shrink: 0; background: var(--accent); color: #06210f; font-weight: 700; font-size: 13px;
    text-decoration: none; padding: 9px 18px; border-radius: 9px; white-space: nowrap;
    align-self: center;
  }}

  .table-wrap {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{
    text-align: left; font-size: 11px; color: var(--text-dim); text-transform: uppercase;
    letter-spacing: 0.4px; font-weight: 700; padding: 12px 16px; border-bottom: 1px solid var(--border);
    background: var(--surface-2);
  }}
  td {{ padding: 12px 16px; font-size: 13.5px; border-bottom: 1px solid var(--border); }}
  tr:last-child td {{ border-bottom: none; }}
  .muted {{ color: var(--text-dim); }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; font-weight: 700; color: var(--amber); }}
  th:last-child {{ text-align: right; }}

  .empty {{ font-size: 13px; color: var(--text-dim); background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; }}

  footer {{ margin-top: 32px; font-size: 11.5px; color: var(--text-dim); line-height: 1.6; }}

  @media (min-width: 700px) {{
    .owner-grid {{ grid-template-columns: repeat(2, 1fr); }}
  }}
  @media (min-width: 1040px) {{
    .page {{ padding: 0 12px; }}
    .stats {{ grid-template-columns: repeat(3, minmax(0, 260px)); }}
    .owner-grid {{ grid-template-columns: repeat(3, 1fr); }}
  }}

  @media (prefers-reduced-motion: reduce) {{
    .owner-card {{ animation: none; opacity: 1; transform: none; }}
  }}
</style>
</head>
<body>
  <div class="page">
    <div class="top">
      <div class="brand">
        <div class="logo">RR</div>
        <div>
          <h1>Rental Renewal Reminders</h1>
          <div class="date">{generated_date}</div>
        </div>
      </div>
      <div class="sync-badge">● Last sync: {last_sync_display}</div>
    </div>

    <div class="stats">
      <div class="stat">
        <div class="label">Tracked</div>
        <div class="value" data-countup="{total_count}">0</div>
      </div>
      <div class="stat {due_stat_class}">
        <div class="label">Action Required</div>
        <div class="value" data-countup="{due_count}">0</div>
      </div>
      <div class="stat">
        <div class="label">Next {upcoming_window} Days</div>
        <div class="value" data-countup="{upcoming_count}">0</div>
      </div>
    </div>

    <h2>Action Queue <span class="count">{owner_group_count}</span></h2>
    {due_section}

    <h2>Upcoming Renewals</h2>
    <div class="table-wrap">
      <table>
        <tr><th>Property</th><th>Owner</th><th>Ends</th><th>Days Left</th></tr>
        {upcoming_section}
      </table>
    </div>

    <footer>
      Tap "Send" to open a pre-filled WhatsApp message from your own number.
      Nothing sends automatically — you review and tap Send yourself.<br>
      Agreements more than 15 days overdue drop off this queue on the
      assumption a phone call, not a text, is the next step.
    </footer>
  </div>

  <script>
    (function () {{
      var reduceMotion = window.matchMedia &&
        window.matchMedia('(prefers-reduced-motion: reduce)').matches;

      document.querySelectorAll('[data-countup]').forEach(function (el) {{
        var target = parseInt(el.getAttribute('data-countup'), 10) || 0;
        if (reduceMotion || target === 0) {{
          el.textContent = target;
          return;
        }}
        var duration = 600;
        var startTime = null;
        function step(ts) {{
          if (startTime === null) startTime = ts;
          var progress = Math.min((ts - startTime) / duration, 1);
          var eased = 1 - Math.pow(1 - progress, 3);
          el.textContent = Math.round(eased * target);
          if (progress < 1) {{
            requestAnimationFrame(step);
          }} else {{
            el.textContent = target;
          }}
        }}
        requestAnimationFrame(step);
      }});
    }})();

    (function () {{
      var cards = document.querySelectorAll('.owner-card');
      cards.forEach(function (card, i) {{
        card.style.animationDelay = (i * 60) + 'ms';
      }});
    }})();
  </script>
</body>
</html>
"""

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

    last_sync_display = get_last_sync_display(log_ws)

    data = sheet.get_all_records()
    cleaned_data = [{k.replace(":", "").strip(): v for k, v in row.items()} for row in data]

    today = date.today()
    due_by_owner = defaultdict(list)
    owner_max_severity = {}
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

        total_count += 1

        days_left = (end_date - today).days
        end_date_display = end_date.strftime("%d %b %Y")

        if ACTIVE_WINDOW_MIN_DAYS <= days_left <= ACTIVE_WINDOW_MAX_DAYS:
            phone = normalize_phone(raw_phone)
            message = build_message(owner, flat, tenant, end_date_display)
            link = wa_link(phone, message)

            pill_class, pill_text, severity = classify_badge(days_left)
            tenant_line = f"Tenant: {html.escape(tenant)} &middot; " if tenant else ""

            property_row_html = PROPERTY_ROW_TEMPLATE.format(
                flat=html.escape(flat),
                pill_class=pill_class,
                pill_text=pill_text,
                tenant_line=tenant_line,
                end_date_display=end_date_display,
                link=link,
            )

            group_key = (owner, phone)
            due_by_owner[group_key].append(property_row_html)
            due_count += 1

            prior = owner_max_severity.get(group_key, -999)
            if severity > prior:
                owner_max_severity[group_key] = severity

            log_row(log_ws, owner, phone, flat, "LISTED", f"days_left={days_left}")

        elif ACTIVE_WINDOW_MAX_DAYS < days_left <= UPCOMING_WINDOW_DAYS:
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

    owner_groups = sorted(
        due_by_owner.items(),
        key=lambda kv: -owner_max_severity.get(kv[0], 0),
    )
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

    upcoming_rows.sort(key=lambda r: r["days_left"])
    upcoming_preview = upcoming_rows[:UPCOMING_PREVIEW_LIMIT]

    generated_date = today.strftime("%d %b %Y")

    due_section = (
        f'<div class="owner-grid">{"".join(owner_cards_html)}</div>'
        if owner_cards_html
        else '<div class="empty">Nothing in the active window right now.</div>'
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
        f"Dashboard generated: {due_count} in active window across {len(owner_groups)} owner(s), "
        f"{len(upcoming_rows)} upcoming in next {UPCOMING_WINDOW_DAYS} days, "
        f"{total_count} total tracked."
    )


if __name__ == "__main__":
    main()

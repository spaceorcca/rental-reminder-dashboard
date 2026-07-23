"""
Rental Renewal Reminder Dashboard Generator
--------------------------------------------
Reads the Google Sheet of rental agreements, finds owners due a reminder
today (30 days before end date, or on the end date), and generates a
static HTML dashboard with one-tap "Send WhatsApp" buttons using wa.me
deep links. The actual send is always a manual tap by a human (you),
so this never risks WhatsApp ban/automation detection.

The generated page is published via GitHub Pages.

Required environment variables:
    GOOGLE_SERVICE_ACCOUNT_JSON  - full contents of the service account JSON key
    SHEET_NAME                   - name of the Google Sheet (default: "Rental Agreements")
"""

import os
import json
import html
from datetime import datetime, date
from urllib.parse import quote

import gspread
from google.oauth2.service_account import Credentials

SHEET_NAME = os.environ.get("SHEET_NAME", "Rental Agreements")
LOG_TAB_NAME = "Log"
OUTPUT_DIR = "docs"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "index.html")

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")


def fail_fast_on_missing_env():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise SystemExit(
            "Missing required environment variable: GOOGLE_SERVICE_ACCOUNT_JSON. "
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


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Rental Renewal Reminders — {generated_date}</title>
<style>
  :root {{
    --bg: #0f1115;
    --card-bg: #171a21;
    --accent: #25d366;
    --accent-dim: #1da851;
    --text: #e8eaed;
    --text-dim: #9aa0a6;
    --border: #2a2e37;
    --urgent: #ff6b6b;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 24px 16px 60px;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  .header {{
    max-width: 640px;
    margin: 0 auto 24px;
  }}
  .header h1 {{
    font-size: 1.4rem;
    margin: 0 0 4px;
  }}
  .header p {{
    color: var(--text-dim);
    margin: 0;
    font-size: 0.9rem;
  }}
  .container {{
    max-width: 640px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }}
  .card {{
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 18px;
  }}
  .card-top {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 10px;
    margin-bottom: 10px;
  }}
  .owner-name {{
    font-size: 1.1rem;
    font-weight: 600;
    margin: 0;
  }}
  .flat-address {{
    color: var(--text-dim);
    font-size: 0.88rem;
    margin: 2px 0 0;
  }}
  .badge {{
    font-size: 0.72rem;
    font-weight: 600;
    padding: 4px 10px;
    border-radius: 999px;
    white-space: nowrap;
  }}
  .badge.due-today {{
    background: rgba(255,107,107,0.15);
    color: var(--urgent);
  }}
  .badge.due-30 {{
    background: rgba(37,211,102,0.15);
    color: var(--accent);
  }}
  .meta-row {{
    font-size: 0.85rem;
    color: var(--text-dim);
    margin-bottom: 14px;
  }}
  .send-btn {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    background: var(--accent);
    color: #06210f;
    font-weight: 600;
    font-size: 0.95rem;
    text-decoration: none;
    padding: 12px;
    border-radius: 10px;
    transition: background 0.15s ease;
  }}
  .send-btn:active {{
    background: var(--accent-dim);
  }}
  .empty-state {{
    max-width: 640px;
    margin: 60px auto;
    text-align: center;
    color: var(--text-dim);
  }}
  .empty-state .big {{
    font-size: 2.5rem;
    margin-bottom: 10px;
  }}
  .footer-note {{
    max-width: 640px;
    margin: 30px auto 0;
    text-align: center;
    color: var(--text-dim);
    font-size: 0.78rem;
    line-height: 1.5;
  }}
</style>
</head>
<body>
  <div class="header">
    <h1>Rental Renewal Reminders</h1>
    <p>Generated {generated_date} · {count} due today</p>
  </div>
  <div class="container">
    {cards}
  </div>
  {empty_state}
  <div class="footer-note">
    Tap "Send WhatsApp" to open a pre-filled message in your own WhatsApp account.
    Nothing is sent automatically — you review and tap Send yourself.
  </div>
</body>
</html>
"""

CARD_TEMPLATE = """
    <div class="card">
      <div class="card-top">
        <div>
          <p class="owner-name">{owner}</p>
          <p class="flat-address">{flat}</p>
        </div>
        <span class="badge {badge_class}">{badge_text}</span>
      </div>
      <div class="meta-row">Ends {end_date_display}{tenant_line}</div>
      <a class="send-btn" href="{link}" target="_blank" rel="noopener">
        Send WhatsApp to {owner}
      </a>
    </div>
"""


def main():
    fail_fast_on_missing_env()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    client = get_sheet_client()
    
    # Opens sheet using default title 'Rental Agreements'
    spreadsheet = client.open(SHEET_NAME)
    sheet = spreadsheet.sheet1
    log_ws = get_or_create_log_tab(spreadsheet)

    data = sheet.get_all_records()
    cleaned_data = [{k.replace(":", "").strip(): v for k, v in row.items()} for row in data]

    today = date.today()
    cards_html = []

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

        days_left = (end_date - today).days
        if days_left not in (30, 0):
            continue

        phone = normalize_phone(raw_phone)
        end_date_display = end_date.strftime("%d-%b-%Y")
        message = build_message(owner, flat, tenant, end_date_display)
        link = wa_link(phone, message)

        badge_class = "due-today" if days_left == 0 else "due-30"
        badge_text = "Ends today" if days_left == 0 else "30 days left"
        tenant_line = f" · Tenant: {html.escape(tenant)}" if tenant else ""

        cards_html.append(
            CARD_TEMPLATE.format(
                owner=html.escape(owner),
                flat=html.escape(flat),
                badge_class=badge_class,
                badge_text=badge_text,
                end_date_display=end_date_display,
                tenant_line=tenant_line,
                link=link,
            )
        )

        log_row(log_ws, owner, phone, flat, "LISTED", f"days_left={days_left}")

    generated_date = today.strftime("%d %b %Y")
    count = len(cards_html)

    if cards_html:
        cards_block = "".join(cards_html)
        empty_state_block = ""
    else:
        cards_block = ""
        empty_state_block = (
            '<div class="empty-state">'
            '<div class="big">✅</div>'
            '<p>No reminders due today. Nothing to send.</p>'
            "</div>"
        )

    final_html = HTML_TEMPLATE.format(
        generated_date=generated_date,
        count=count,
        cards=cards_block,
        empty_state=empty_state_block,
    )

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(final_html)

    print(f"Dashboard generated with {count} reminder(s) due today.")


if __name__ == "__main__":
    main()

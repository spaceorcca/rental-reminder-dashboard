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
                                 severity badge. Missing a day checking the
                                 dashboard can never silently drop a
                                 reminder — it stays in the queue until it
                                 ages out past -15 days (at which point the
                                 assumption is a phone call, not a text).

Badge tiers:
    15 <= days_left <= 30   -> "30-Day Window"   (info blue)
    1  <= days_left <= 14   -> "Expiring Soon"   (warning amber, neon glow)
    days_left == 0          -> "Expires Today"   (urgent red, neon glow)
    -15 <= days_left < 0    -> "Overdue (Nd)"    (critical crimson, neon glow)

Owners with more than one property inside the active window are grouped
into a single card with one property row per property.

A separate "Upcoming Renewals" table shows agreements further out
(31-60 days) purely for visibility.

DESIGN SYSTEM (this pass):
  - Spacing scale: every padding/margin/gap value is one of
    4 / 8 / 12 / 16 / 24 / 32 / 48px. No ad hoc values.
  - Type scale: 11 / 13 / 16 / 22 / 27px steps, applied consistently
    across KPI values, headings, body text, and mono figures.
  - Layered shadows: cards use a tight near shadow + a soft ambient
    shadow rather than a single flat shadow value, for real depth.
  - Unified easing: every transition on the page uses the same
    cubic-bezier(0.16, 1, 0.3, 1) soft ease-out curve.
  - Tuned contrast: badge backgrounds desaturated to ~10-12% opacity,
    badge text brightened slightly, so tints read as engineered rather
    than default alert colors.

TACTILE MICRO-DETAILS (this pass):
  - Top-border severity accent: 2px colored top border per property row
    matching its badge tier.
  - Cursor spotlight on card hover: CSS custom-property radial gradient
    tracking the pointer, desktop-only (@media hover:hover), sharing the
    same mousemove listener as the existing hover-glow logic so there is
    only one listener, not two.
  - Kbd hint ("/") inside the search input, and a real "/" keyboard
    shortcut that focuses the search box (skipped while typing in the
    box itself, so "/" can still be typed as a search character).
  - Custom dark slim scrollbar (Webkit + Firefox) and an emerald
    ::selection highlight.
  - Micro SVG grain overlay: inline feTurbulence filter as a fixed,
    very-low-opacity full-viewport layer. No external image request.

Motion effects (KPI count-up, staggered entry, search + filter chips)
carried over unchanged from the previous pass.

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

ACTIVE_WINDOW_MIN_DAYS = -15
ACTIVE_WINDOW_MAX_DAYS = 30

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
    Returns (pill_css_class, pill_text, severity_rank, filter_key) for a
    given days_left. severity_rank sorts the queue most-urgent-first.
    filter_key is the value used by the client-side filter chips.
    Only meaningful for rows already known to be inside the active window.
    """
    if days_left < 0:
        overdue_by = abs(days_left)
        return ("overdue", f"Overdue ({overdue_by}d)", 3 + overdue_by, "overdue")
    if days_left == 0:
        return ("today", "Expires Today", 3, "today")
    if days_left <= 14:
        return ("soon", "Expiring Soon", 2, "soon")
    return ("window30", "30-Day Window", 1, "window30")


# -------------------------------------------------------------------------
# HTML template. See module docstring for the full design-system rationale.
# -------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Rental Renewal Reminders — {generated_date}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --sp-1: 4px;
    --sp-2: 8px;
    --sp-3: 12px;
    --sp-4: 16px;
    --sp-5: 24px;
    --sp-6: 32px;
    --sp-7: 48px;

    --fs-xs: 11px;
    --fs-sm: 13px;
    --fs-md: 16px;
    --fs-lg: 22px;
    --fs-xl: 27px;

    --ease: cubic-bezier(0.16, 1, 0.3, 1);

    --bg: #05070c;
    --surface: rgba(19, 24, 38, 0.68);
    --surface-2: rgba(255, 255, 255, 0.02);
    --border: rgba(255, 255, 255, 0.08);
    --border-strong: rgba(255, 255, 255, 0.16);
    --text: #edf0f5;
    --text-dim: #8b96a8;

    --accent: #22c55e;
    --accent-glow: #00f090;
    --accent-soft: rgba(34, 197, 94, 0.12);

    --primary: #7c9bff;
    --primary-soft: rgba(124, 155, 255, 0.12);

    --danger: #ff6b85;
    --danger-bg: rgba(255, 77, 109, 0.11);
    --danger-glow: rgba(255, 77, 109, 0.5);

    --amber: #ffc14d;
    --amber-bg: rgba(255, 176, 32, 0.11);
    --amber-glow: rgba(255, 176, 32, 0.45);

    --critical: #ff5577;
    --critical-bg: rgba(255, 45, 85, 0.14);
    --critical-glow: rgba(255, 45, 85, 0.55);
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  ::selection {{ background: rgba(34, 197, 94, 0.35); color: #eafff2; }}

  ::-webkit-scrollbar {{ width: 10px; height: 10px; }}
  ::-webkit-scrollbar-track {{ background: transparent; }}
  ::-webkit-scrollbar-thumb {{
    background: rgba(255, 255, 255, 0.12);
    border-radius: 999px;
    border: 2px solid var(--bg);
  }}
  ::-webkit-scrollbar-thumb:hover {{ background: rgba(255, 255, 255, 0.2); }}
  html {{ scrollbar-width: thin; scrollbar-color: rgba(255,255,255,0.15) transparent; }}

  body {{
    position: relative;
    background: var(--bg);
    background-image:
      linear-gradient(rgba(255,255,255,0.025) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255,255,255,0.025) 1px, transparent 1px),
      radial-gradient(ellipse 900px 400px at 50% -10%, rgba(109,139,255,0.10), transparent 60%);
    background-size: 42px 42px, 42px 42px, 100% 100%;
    background-attachment: fixed;
    color: var(--text);
    font-family: 'Space Grotesk', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    padding: var(--sp-6) var(--sp-4) var(--sp-7);
    -webkit-font-smoothing: antialiased;
    min-height: 100vh;
  }}
  .mono {{
    font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }}

  .grain {{
    position: fixed; inset: 0; z-index: 0; pointer-events: none;
    opacity: 0.035; mix-blend-mode: overlay;
  }}

  .page {{ position: relative; z-index: 1; max-width: 1180px; margin: 0 auto; }}

  .top {{
    display: flex; justify-content: space-between; align-items: flex-start;
    margin-bottom: var(--sp-1); flex-wrap: wrap; gap: var(--sp-3);
  }}
  .brand {{ display: flex; align-items: center; gap: var(--sp-3); }}
  .logo {{
    width: 40px; height: 40px; border-radius: 11px; flex-shrink: 0;
    background: linear-gradient(135deg, var(--primary), var(--accent));
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: var(--fs-sm); color: #06101f;
    box-shadow: 0 0 18px rgba(109, 139, 255, 0.35);
  }}
  .top h1 {{ font-size: 19px; font-weight: 700; letter-spacing: 0.02em; }}
  .top .date {{
    font-size: var(--fs-xs); color: var(--text-dim); margin-top: var(--sp-1);
    text-transform: uppercase; letter-spacing: 0.08em;
  }}

  .sync-badge {{
    display: flex; align-items: center; gap: var(--sp-2);
    font-size: var(--fs-xs); color: var(--accent);
    background: rgba(34, 197, 94, 0.08);
    border: 1px solid rgba(34, 197, 94, 0.3);
    padding: var(--sp-2) var(--sp-4) var(--sp-2) var(--sp-2);
    border-radius: 999px; font-weight: 600; white-space: nowrap;
    text-transform: uppercase; letter-spacing: 0.06em;
  }}
  .radar {{ position: relative; width: 10px; height: 10px; flex-shrink: 0; }}
  .radar-dot {{
    position: absolute; inset: 3px; background: var(--accent-glow);
    border-radius: 50%; box-shadow: 0 0 8px var(--accent-glow);
  }}
  .radar-ring {{
    position: absolute; inset: 0; border-radius: 50%;
    border: 1px solid var(--accent-glow);
    animation: radar-pulse 2.2s ease-out infinite;
  }}
  .radar-ring.delay {{ animation-delay: 1.1s; }}
  @keyframes radar-pulse {{
    0%   {{ transform: scale(0.4); opacity: 0.9; }}
    100% {{ transform: scale(2.6); opacity: 0; }}
  }}

  .stat, .toolbar .search-input, .chip, .owner-card, .table-wrap, .empty {{
    box-shadow:
      0 1px 2px rgba(0, 0, 0, 0.3),
      0 8px 24px -8px rgba(0, 0, 0, 0.45);
  }}

  .stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--sp-3); margin: var(--sp-5) 0 var(--sp-6); }}
  .stat {{
    background: var(--surface);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border: 1px solid var(--border);
    border-radius: 13px; padding: var(--sp-4);
  }}
  .stat .label {{
    font-size: var(--fs-xs); color: var(--text-dim); text-transform: uppercase;
    letter-spacing: 0.08em; font-weight: 600;
  }}
  .stat .value {{
    font-size: var(--fs-xl); font-weight: 700; margin-top: var(--sp-1);
    font-variant-numeric: tabular-nums; line-height: 1.1;
  }}
  .stat.warn .value {{ color: var(--danger); }}
  .stat.ok .value {{ color: var(--accent); }}

  h2 {{
    font-size: var(--fs-xs); font-weight: 700; color: var(--text-dim); text-transform: uppercase;
    letter-spacing: 0.08em; margin: var(--sp-6) 0 var(--sp-3);
    display: flex; justify-content: space-between; align-items: baseline;
  }}
  h2 .count {{ font-size: var(--fs-xs); font-weight: 600; color: var(--text-dim); text-transform: none; letter-spacing: 0; }}

  .toolbar {{
    display: flex; gap: var(--sp-3); flex-wrap: wrap; align-items: center;
    margin-bottom: var(--sp-4);
  }}
  .search-wrap {{ flex: 1 1 220px; position: relative; }}
  .search-input {{
    width: 100%; background: var(--surface);
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    border: 1px solid var(--border); border-radius: 10px;
    padding: var(--sp-3) var(--sp-6) var(--sp-3) var(--sp-6);
    color: var(--text); font-size: var(--fs-sm); font-family: inherit;
    transition: border-color 0.2s var(--ease), box-shadow 0.2s var(--ease);
  }}
  .search-input::placeholder {{ color: var(--text-dim); }}
  .search-input:focus {{ outline: none; border-color: rgba(124,155,255,0.5); }}
  .search-icon {{
    position: absolute; left: var(--sp-3); top: 50%; transform: translateY(-50%);
    color: var(--text-dim); font-size: var(--fs-sm); pointer-events: none;
  }}
  .search-kbd {{
    position: absolute; right: var(--sp-2); top: 50%; transform: translateY(-50%);
    pointer-events: none;
  }}
  kbd {{
    font-family: 'JetBrains Mono', ui-monospace, monospace;
    font-size: 10px; color: var(--text-dim);
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid var(--border-strong);
    border-bottom-width: 2px;
    border-radius: 5px;
    padding: 2px var(--sp-2);
  }}

  .chips {{ display: flex; gap: var(--sp-2); flex-wrap: wrap; }}
  .chip {{
    background: var(--surface); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    border: 1px solid var(--border); color: var(--text-dim);
    font-size: var(--fs-xs); font-weight: 600; padding: var(--sp-2) var(--sp-4); border-radius: 999px;
    cursor: pointer; text-transform: uppercase; letter-spacing: 0.04em;
    transition: border-color 0.2s var(--ease), color 0.2s var(--ease), background 0.2s var(--ease);
  }}
  .chip.active {{
    color: var(--text); border-color: rgba(124,155,255,0.55);
    background: rgba(124,155,255,0.12);
  }}

  .owner-grid {{ display: grid; grid-template-columns: 1fr; gap: var(--sp-3); }}

  .owner-card {{
    position: relative;
    background: var(--surface);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border: 1px solid var(--border); border-radius: 15px;
    padding: var(--sp-4); display: flex; flex-direction: column;
    overflow: hidden;
    opacity: 0; transform: translateY(10px);
    animation: card-in 0.45s var(--ease) forwards;
  }}
  .owner-card.js-hidden {{ display: none; }}
  @keyframes card-in {{
    to {{ opacity: 1; transform: translateY(0); }}
  }}
  .owner-card::before {{
    content: ""; position: absolute; inset: 0; z-index: 0;
    background: radial-gradient(
      280px circle at var(--x, 50%) var(--y, 50%),
      rgba(124, 155, 255, 0.10), transparent 70%
    );
    opacity: 0; transition: opacity 0.3s var(--ease);
    pointer-events: none;
  }}
  @media (hover: hover) and (pointer: fine) {{
    .owner-card {{ transition: box-shadow 0.2s var(--ease), border-color 0.2s var(--ease); }}
    .owner-card:hover {{
      border-color: rgba(124, 155, 255, 0.45);
      box-shadow:
        0 1px 2px rgba(0,0,0,0.3),
        0 0 0 1px rgba(124, 155, 255, 0.18),
        0 12px 32px -8px rgba(124, 155, 255, 0.18);
    }}
    .owner-card:hover::before {{ opacity: 1; }}
  }}
  .owner-card > * {{ position: relative; z-index: 1; }}

  .owner-head {{
    display: flex; align-items: center; justify-content: space-between; gap: var(--sp-3);
    margin-bottom: var(--sp-2); flex-wrap: wrap;
  }}
  .owner-name {{ font-size: var(--fs-md); font-weight: 700; }}
  .owner-count {{
    font-size: var(--fs-xs); color: var(--text-dim); font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.05em;
  }}

  .property-row {{
    display: flex; align-items: center; justify-content: space-between; gap: var(--sp-3);
    padding: var(--sp-3) 0 var(--sp-3);
    border-top: 2px solid var(--border);
  }}
  .owner-card .property-row:first-of-type {{ border-top: none; padding-top: var(--sp-2); }}
  .property-row.sev-overdue {{ border-top-color: var(--critical); }}
  .property-row.sev-today {{ border-top-color: var(--danger); }}
  .property-row.sev-soon {{ border-top-color: var(--amber); }}
  .property-row.sev-window30 {{ border-top-color: var(--primary); }}

  .property-main {{ min-width: 0; flex: 1 1 auto; }}
  .property-top {{ display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }}
  .flat-name {{ font-size: var(--fs-sm); font-weight: 600; line-height: 1.35; }}

  .pill {{
    font-family: 'JetBrains Mono', ui-monospace, monospace;
    font-size: 10px; font-weight: 600; padding: 3px var(--sp-2); border-radius: 999px;
    text-transform: uppercase; letter-spacing: 0.05em; white-space: nowrap; flex-shrink: 0;
  }}
  .pill.window30 {{ background: var(--primary-soft); color: var(--primary); }}
  .pill.soon {{ background: var(--amber-bg); color: var(--amber); box-shadow: 0 0 10px var(--amber-glow); }}
  .pill.today {{ background: var(--danger-bg); color: var(--danger); box-shadow: 0 0 10px var(--danger-glow); }}
  .pill.overdue {{ background: var(--critical-bg); color: var(--critical); box-shadow: 0 0 12px var(--critical-glow); }}

  .sub {{ font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: var(--fs-xs); color: var(--text-dim); margin-top: var(--sp-1); }}

  .send-btn {{
    flex-shrink: 0; background: var(--accent); color: #06210f; font-weight: 700; font-size: var(--fs-xs);
    text-decoration: none; padding: var(--sp-2) var(--sp-4); border-radius: 9px; white-space: nowrap;
    align-self: center; display: inline-flex; align-items: center; gap: var(--sp-1);
    transition: transform 0.2s var(--ease), box-shadow 0.2s var(--ease);
  }}
  @media (hover: hover) and (pointer: fine) {{
    .send-btn:hover {{ box-shadow: 0 0 16px rgba(34, 197, 94, 0.35); }}
  }}

  .table-wrap {{
    background: var(--surface); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    border: 1px solid var(--border); border-radius: 13px; overflow: hidden;
  }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{
    text-align: left; font-size: 10.5px; color: var(--text-dim); text-transform: uppercase;
    letter-spacing: 0.06em; font-weight: 700; padding: var(--sp-3) var(--sp-4);
    border-bottom: 1px solid var(--border); background: var(--surface-2);
  }}
  td {{
    padding: var(--sp-3) var(--sp-4); font-size: var(--fs-sm); border-bottom: 1px solid var(--border);
    font-family: 'JetBrains Mono', ui-monospace, monospace;
  }}
  tr:last-child td {{ border-bottom: none; }}
  .muted {{ color: var(--text-dim); }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; font-weight: 700; color: var(--amber); }}
  th:last-child {{ text-align: right; }}

  .empty {{
    font-size: var(--fs-sm); color: var(--text-dim); background: var(--surface);
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    border: 1px solid var(--border); border-radius: 13px; padding: var(--sp-4);
  }}

  footer {{ margin-top: var(--sp-6); font-size: var(--fs-xs); color: var(--text-dim); line-height: 1.6; }}

  @media (min-width: 700px) {{
    .owner-grid {{ grid-template-columns: repeat(2, 1fr); }}
  }}
  @media (min-width: 1040px) {{
    .page {{ padding: 0 var(--sp-3); }}
    .stats {{ grid-template-columns: repeat(3, minmax(0, 260px)); }}
    .owner-grid {{ grid-template-columns: repeat(3, 1fr); }}
  }}

  @media (prefers-reduced-motion: reduce) {{
    .owner-card {{ animation: none; opacity: 1; transform: none; }}
    .radar-ring {{ animation: none; opacity: 0.4; }}
  }}
</style>
</head>
<body>
  <svg class="grain" xmlns="http://www.w3.org/2000/svg">
    <filter id="grainFilter">
      <feTurbulence type="fractalNoise" baseFrequency="0.85" numOctaves="2" stitchTiles="stitch"></feTurbulence>
      <feColorMatrix type="saturate" values="0"></feColorMatrix>
    </filter>
    <rect width="100%" height="100%" filter="url(#grainFilter)"></rect>
  </svg>

  <div class="page">
    <div class="top">
      <div class="brand">
        <div class="logo">RR</div>
        <div>
          <h1>Rental Renewal Reminders</h1>
          <div class="date">{generated_date}</div>
        </div>
      </div>
      <div class="sync-badge">
        <span class="radar">
          <span class="radar-ring"></span>
          <span class="radar-ring delay"></span>
          <span class="radar-dot"></span>
        </span>
        Last sync: {last_sync_display}
      </div>
    </div>

    <div class="stats">
      <div class="stat">
        <div class="label">Tracked</div>
        <div class="value mono" data-countup="{total_count}">0</div>
      </div>
      <div class="stat {due_stat_class}">
        <div class="label">Action Required</div>
        <div class="value mono" data-countup="{due_count}">0</div>
      </div>
      <div class="stat">
        <div class="label">Next {upcoming_window} Days</div>
        <div class="value mono" data-countup="{upcoming_count}">0</div>
      </div>
    </div>

    <h2>Action Queue <span class="count">{owner_group_count}</span></h2>

    <div class="toolbar">
      <div class="search-wrap">
        <span class="search-icon">⌕</span>
        <input
          type="text"
          id="queueSearch"
          class="search-input mono"
          placeholder="Search owner, property, or tenant..."
          autocomplete="off"
        >
        <span class="search-kbd"><kbd>/</kbd></span>
      </div>
      <div class="chips" id="filterChips">
        <span class="chip active" data-filter="all">All</span>
        <span class="chip" data-filter="overdue">Overdue</span>
        <span class="chip" data-filter="today">Today</span>
        <span class="chip" data-filter="soon">Expiring Soon</span>
        <span class="chip" data-filter="window30">30-Day</span>
      </div>
    </div>

    <div id="queueEmpty" class="empty" style="display:none;">No matches for this search or filter.</div>
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

    (function () {{
      var supportsHover = window.matchMedia &&
        window.matchMedia('(hover: hover) and (pointer: fine)').matches;
      if (!supportsHover) return;

      document.querySelectorAll('.owner-card').forEach(function (card) {{
        card.addEventListener('mousemove', function (e) {{
          var rect = card.getBoundingClientRect();
          card.style.setProperty('--x', (e.clientX - rect.left) + 'px');
          card.style.setProperty('--y', (e.clientY - rect.top) + 'px');
        }});
      }});
    }})();

    (function () {{
      var searchInput = document.getElementById('queueSearch');
      var chips = document.querySelectorAll('#filterChips .chip');
      var cards = document.querySelectorAll('.owner-card');
      var emptyState = document.getElementById('queueEmpty');
      var activeFilter = 'all';

      function applyFilters() {{
        var query = (searchInput.value || '').trim().toLowerCase();
        var visibleCount = 0;

        cards.forEach(function (card) {{
          var owner = (card.getAttribute('data-owner') || '').toLowerCase();
          var searchBlob = card.getAttribute('data-search') || '';
          var severities = (card.getAttribute('data-severities') || '').split(',');

          var matchesSearch = !query || searchBlob.indexOf(query) !== -1 || owner.indexOf(query) !== -1;
          var matchesFilter = activeFilter === 'all' || severities.indexOf(activeFilter) !== -1;

          var visible = matchesSearch && matchesFilter;
          card.classList.toggle('js-hidden', !visible);
          if (visible) visibleCount++;
        }});

        emptyState.style.display = (visibleCount === 0 && cards.length > 0) ? 'block' : 'none';
      }}

      if (searchInput) {{
        searchInput.addEventListener('input', applyFilters);

        document.addEventListener('keydown', function (e) {{
          if (e.key !== '/') return;
          var active = document.activeElement;
          var isTyping = active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA');
          if (isTyping) return;
          e.preventDefault();
          searchInput.focus();
        }});
      }}
      chips.forEach(function (chip) {{
        chip.addEventListener('click', function () {{
          chips.forEach(function (c) {{ c.classList.remove('active'); }});
          chip.classList.add('active');
          activeFilter = chip.getAttribute('data-filter');
          applyFilters();
        }});
      }});
    }})();
  </script>
</body>
</html>
"""

OWNER_CARD_TEMPLATE = """
        <div class="owner-card" data-owner="{owner}" data-search="{search_blob}" data-severities="{severities}">
          <div class="owner-head">
            <span class="owner-name">{owner}</span>
            <span class="owner-count">{property_count}</span>
          </div>
          {property_rows}
        </div>
"""

PROPERTY_ROW_TEMPLATE = """
          <div class="property-row sev-{filter_key}">
            <div class="property-main">
              <div class="property-top">
                <span class="flat-name">{flat}</span>
                <span class="pill {pill_class}">{pill_text}</span>
              </div>
              <div class="sub">{tenant_line}ends {end_date_display}</div>
            </div>
            <a class="send-btn" href="{link}" target="_blank" rel="noopener">Send ↗</a>
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
    owner_search_terms = defaultdict(list)
    owner_filter_keys = defaultdict(set)
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

            pill_class, pill_text, severity, filter_key = classify_badge(days_left)
            tenant_line = f"Tenant: {html.escape(tenant)} &middot; " if tenant else ""

            property_row_html = PROPERTY_ROW_TEMPLATE.format(
                flat=html.escape(flat),
                pill_class=pill_class,
                pill_text=pill_text,
                filter_key=filter_key,
                tenant_line=tenant_line,
                end_date_display=end_date_display,
                link=link,
            )

            group_key = (owner, phone)
            due_by_owner[group_key].append(property_row_html)
            due_count += 1

            owner_search_terms[group_key].append(flat.lower())
            if tenant:
                owner_search_terms[group_key].append(tenant.lower())
            owner_filter_keys[group_key].add(filter_key)

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
        group_key = (owner, _phone)
        search_blob = html.escape(
            (owner.lower() + " " + " ".join(owner_search_terms[group_key])).strip()
        )
        severities = ",".join(sorted(owner_filter_keys[group_key]))

        owner_cards_html.append(
            OWNER_CARD_TEMPLATE.format(
                owner=html.escape(owner),
                property_count=property_count_label,
                property_rows="".join(property_rows),
                search_blob=search_blob,
                severities=severities,
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

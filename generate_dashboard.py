#!/usr/bin/env python3
"""
Rental Renewal Reminder Dashboard Generator
===========================================
Reads the "Rental Agreements" Google Sheet and writes a static HTML dashboard
(docs/index.html) listing every agreement inside the action window, each with
a one-tap pre-filled WhatsApp link (wa.me). Nothing is ever sent automatically;
a human taps Send, so there is no automation/ban risk.

Usage
-----
    python generate_dashboard.py                 # real run (needs env vars)
    python generate_dashboard.py --demo          # sample data -> demo.html
    python generate_dashboard.py --demo --empty  # preview the empty state

Env vars (real run): GOOGLE_SERVICE_ACCOUNT_JSON, SPREADSHEET_ID
Deps: gspread, google-auth

Action window (continuous, never exact-day matching):
    -15 <= days_left <= 30 -> Action Queue, always visible
        15..30 -> "30-Day Window"     1..14 -> "Expiring Soon"
        0      -> "Expires Today"     -15..-1 -> "Overdue (Nd)"
    31..60                -> "Upcoming Renewals" grid

Sheet columns (header matching ignores case, spaces and colons):
    Owner Name | Tenant Name | Flat Address | Phone Number | End Date
"""

import argparse
import html
import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

# ── Timezone (GitHub Actions runs in UTC; agreements are IST) ────────────────
try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # tzdata missing (e.g. Windows) -> fixed +05:30 offset
    IST = timezone(timedelta(hours=5, minutes=30), "IST")

DATE_FORMATS = (
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
    "%d-%b-%Y", "%d %b %Y", "%d %B %Y", "%d-%B-%Y",
)

# ── Config ───────────────────────────────────────────────────────────────────
LOG_TAB_NAME = "Log"
OUTPUT_DIR = "docs"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "index.html")
DEMO_OUTPUT_FILE = "demo.html"

ACTIVE_WINDOW_MIN_DAYS = -15
ACTIVE_WINDOW_MAX_DAYS = 30
UPCOMING_WINDOW_DAYS = 60
UPCOMING_PREVIEW_LIMIT = 10
STALE_AFTER_HOURS = 36          # dashboard warns if older than this

CONTACT_NAME = "Talib"
CONTACT_NUMBER = "7020204238"

REQUIRED_COLUMNS = ("ownername", "flataddress", "phonenumber", "enddate")

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")

LOG_BUFFER: list = []  # flushed once at the end (Sheets allows ~60 writes/min)


def esc(value) -> str:
    return html.escape(str(value), quote=True)


# ─────────────────────────────────────────────────────────────────────────────
# Data model & helpers
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Agreement:
    owner: str
    tenant: str
    flat: str
    phone_raw: str
    phone: "str | None"
    end_date: date
    days_left: int


def log(owner, phone, flat, status, details) -> None:
    LOG_BUFFER.append([
        datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        owner, phone, flat, status, details,
    ])


def norm_key(header) -> str:
    return re.sub(r"[^a-z]", "", str(header).lower())


def parse_date(raw) -> "date | None":
    s = str(raw).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def normalize_phone(raw) -> "str | None":
    """Return e.g. '919876543210', or None if it isn't a valid Indian number."""
    digits = "".join(c for c in str(raw) if c.isdigit())
    if len(digits) == 10:
        return "91" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "91" + digits[1:]
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    return None


def classify_badge(days_left: int) -> "tuple[str, str]":
    """Returns (tier_key, label)."""
    if days_left < 0:
        return "overdue", f"Overdue ({abs(days_left)}d)"
    if days_left == 0:
        return "today", "Expires today"
    if days_left <= 14:
        return "soon", "Expiring soon"
    return "window30", "30-day window"


def severity(days_left: int) -> int:
    """Higher = more urgent; strictly monotonic so ties sort cleanly."""
    return 1000 - days_left


def build_message(owner, flat, tenant, end_display, days_left) -> str:
    tenant_text = f" (occupied by *{tenant}*)" if tenant else ""
    if days_left < 0:
        when = f"ended on \u2726 *{end_display}*"
    elif days_left == 0:
        when = f"ends today (\u2726 *{end_display}*)"
    else:
        when = f"will end on \u2726 *{end_display}*"
    greeting = owner if owner else "there"
    return (
        f"\u2726 Hi {greeting}!\n\n"
        f"Your rental agreement for \u2726 *{flat}*{tenant_text} {when}.\n"
        f"Let's make sure everything stays smooth and stress-free!\n\n"
        f"\u2726 Why renewing is the best choice:\n"
        f"\u27a4 Avoid last-minute hassle\n"
        f"\u27a4 Keep your property secure\n"
        f"\u27a4 Enjoy uninterrupted rental income\n"
        f"\u27a4 Ensure peace of mind with a confirmed extension\n\n"
        f"We truly value this great partnership and want to keep things "
        f"simple and beneficial for you.\n\n"
        f"Looking forward to continuing this positive experience!\n\n"
        f"\u2726 Cheers,\n"
        f"\u2726 {CONTACT_NAME}\n"
        f"\u2726 Contact: {CONTACT_NUMBER}"
    )


def wa_link(phone: str, message: str) -> str:
    return f"https://wa.me/{phone}?text={quote(message, safe='')}"


# ─────────────────────────────────────────────────────────────────────────────
# Google Sheets
# ─────────────────────────────────────────────────────────────────────────────

def fail_fast_on_missing_env() -> None:
    missing = [n for n, v in (("GOOGLE_SERVICE_ACCOUNT_JSON", GOOGLE_SERVICE_ACCOUNT_JSON),
                              ("SPREADSHEET_ID", SPREADSHEET_ID)) if not v]
    if missing:
        raise SystemExit(
            f"Missing env var(s): {', '.join(missing)}. "
            "Set them as GitHub Actions secrets (or export them locally)."
        )


def with_retry(fn, attempts: int = 3, delay: float = 2.0):
    import gspread
    for i in range(attempts):
        try:
            return fn()
        except gspread.exceptions.APIError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (2 ** i))


def get_spreadsheet():
    import gspread
    from google.oauth2.service_account import Credentials
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON), scopes=scope)
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)


def get_or_create_log_tab(spreadsheet):
    import gspread
    try:
        return spreadsheet.worksheet(LOG_TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=LOG_TAB_NAME, rows=2000, cols=6)
        ws.append_row(["Timestamp", "Owner", "Phone", "Flat", "Status", "Details"])
        return ws


def rows_from_values(values) -> "list[dict]":
    """Header-tolerant rows (survives blank/duplicate headers, keeps strings)."""
    if not values:
        return []
    keys = [norm_key(h) for h in values[0]]
    missing = [c for c in REQUIRED_COLUMNS if c not in keys]
    if missing:
        raise SystemExit(f"Sheet is missing required column(s): {', '.join(missing)}")
    rows = []
    for raw in values[1:]:
        d: dict = {}
        for k, v in zip(keys, raw):
            if k and k not in d:
                d[k] = str(v).strip()
        if any(d.values()):
            rows.append(d)
    return rows


def parse_agreements(rows, today: date) -> "list[Agreement]":
    out = []
    for r in rows:
        owner = r.get("ownername", "")
        tenant = r.get("tenantname", "")
        flat = r.get("flataddress", "")
        raw_phone = r.get("phonenumber", "")
        end_raw = r.get("enddate", "")

        if not end_raw:
            log(owner, raw_phone, flat, "SKIPPED", "Missing end date")
            continue
        end = parse_date(end_raw)
        if end is None:
            log(owner, raw_phone, flat, "SKIPPED", f"Unparseable date: {end_raw!r}")
            continue
        phone = normalize_phone(raw_phone) if raw_phone else None
        out.append(Agreement(owner, tenant, flat, raw_phone, phone, end,
                             (end - today).days))
    return out


def demo_agreements(today: date, empty: bool = False) -> "list[Agreement]":
    if empty:
        return []
    def mk(owner, tenant, flat, phone, offset):
        end = today + timedelta(days=offset)
        return Agreement(owner, tenant, flat, phone, normalize_phone(phone), end, offset)
    return [
        mk("Aarav Mehta", "Kabir Shah", "A-402, Lotus Heights, Baner, Pune", "9000000001", -9),
        mk("Sneha Kulkarni", "Rohan Iyer", "12, Green Acres, Kothrud, Pune", "9000000002", 0),
        mk("Vikram Rao", "Neha Joshi", "B-101, Skyline Residency, Wakad, Pune", "9000000003", 3),
        mk("Vikram Rao", "Imran Sheikh", "C-207, Skyline Residency, Wakad, Pune", "9000000003", 12),
        mk("Vikram Rao", "", "Shop 4, Skyline Arcade, Wakad, Pune", "9000000003", 27),
        mk("Priya Deshmukh", "Ankit Verma",
           "Flat 1203, Tower C, Emerald Towers Phase 2, Near Symbiosis Road, Viman Nagar, Pune 411014",
           "9000000004", 25),
        mk("Rahul Bhosale", "Meera Nair", "Row House 7, Palm Grove, Hinjewadi, Pune", "12345", 6),
        mk("Sandeep More", "Tara Menon", "F-3, Sunrise Court, Kalyani Nagar, Pune", "9000000006", 35),
        mk("Anjali Gupta", "Dev Malhotra", "Villa 9, Orchid Enclave, Bavdhan, Pune", "9000000007", 41),
        mk("Rohit Jadhav", "Pooja Kapoor", "A-8, Maple Court, Pashan, Pune", "9000000008", 58),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# HTML fragments
# ─────────────────────────────────────────────────────────────────────────────

def _svg(inner: str, size: int = 16) -> str:
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{inner}</svg>')


ICON_BOLT = _svg('<path d="M13 2 3 14h9l-1 8 10-12h-9z"/>', 18)
ICON_ALERT = _svg('<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>'
                  '<path d="M12 9v4M12 17h.01"/>', 18)
ICON_CAL = _svg('<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>', 18)
ICON_COPY = _svg('<rect x="9" y="9" width="13" height="13" rx="2"/>'
                 '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>', 16)
ICON_SEARCH = _svg('<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>', 15)
ICON_CLOSE = _svg('<path d="M18 6 6 18M6 6l12 12"/>', 14)
ICON_WA = ('<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
           '<path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297'
           '-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48'
           '-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298'
           '-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51'
           '-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 '
           '1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 '
           '1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124'
           '-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998'
           '-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 '
           '6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 '
           '11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305'
           '-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 '
           '00-3.48-8.413Z"/></svg>')

TIER_ORDER = ("overdue", "today", "soon", "window30")
TIER_NAMES = {"overdue": "Overdue", "today": "Today", "soon": "Expiring soon", "window30": "30-day"}


def owner_initial(owner: str) -> str:
    for ch in owner:
        if ch.isalnum():
            return ch.upper()
    return "?"


def render_row(a: Agreement) -> str:
    tier, label = classify_badge(a.days_left)
    end_disp = a.end_date.strftime("%d %b %Y")
    message = build_message(a.owner, a.flat, a.tenant, end_disp, a.days_left)
    owner_disp = a.owner or "Unnamed owner"

    if a.days_left == 0:
        days_txt, days_sub = "Today", ""
    elif a.days_left < 0:
        days_txt, days_sub = f"{abs(a.days_left)}d", "overdue"
    else:
        days_txt, days_sub = f"{a.days_left}d", "left"

    if a.phone:
        action = (f'<a class="send-btn" href="{esc(wa_link(a.phone, message))}" target="_blank" '
                  f'rel="noopener noreferrer">{ICON_WA}Send WhatsApp</a>')
    else:
        action = ('<span class="send-btn disabled" role="note">'
                  'No valid number \u2013 fix in sheet</span>')

    tenant_line = f'<div class="tenant">Tenant: {esc(a.tenant)}</div>' if a.tenant else ""
    blob = f"{owner_disp} {a.flat} {a.tenant}".lower()

    return (
        f'<div class="property-row" data-sev="{tier}" data-search="{esc(blob)}" data-msg="{esc(message)}">'
        f'<div class="row-top">'
        f'<div class="row-main"><div class="flat-name">{esc(a.flat) or "No address"}</div>{tenant_line}</div>'
        f'<div class="days"><span class="days-num">{days_txt}</span>'
        f'{f"<small>{days_sub}</small>" if days_sub else ""}</div></div>'
        f'<div class="row-meta"><span class="pill {tier}">{esc(label)}</span>'
        f'<span class="end-date">Ends {end_disp}</span></div>'
        f'<div class="row-actions">'
        f'<button type="button" class="icon-btn copy-btn" aria-label="Copy message" '
        f'title="Copy WhatsApp message">{ICON_COPY}</button>{action}</div></div>'
    )


def render_card(owner: str, rows: "list[Agreement]", idx: int) -> str:
    rows = sorted(rows, key=lambda a: -severity(a.days_left))
    worst = classify_badge(rows[0].days_left)[0]
    n = len(rows)
    owner_disp = owner or "Unnamed owner"
    return (
        f'<article class="owner-card" data-worst="{worst}" style="--i:{min(idx, 12)}">'
        f'<header class="owner-head"><div class="owner-avatar" aria-hidden="true">'
        f'{esc(owner_initial(owner))}</div>'
        f'<h3 class="owner-name">{esc(owner_disp)}</h3>'
        f'<span class="owner-count">{n} {"property" if n == 1 else "properties"}</span></header>'
        f'<div class="rows">{"".join(render_row(a) for a in rows)}</div></article>'
    )


def render_upcoming_row(a: Agreement) -> str:
    span = UPCOMING_WINDOW_DAYS - ACTIVE_WINDOW_MAX_DAYS
    pct = max(0.0, min(100.0, (UPCOMING_WINDOW_DAYS - a.days_left) / span * 100))
    return (
        f'<tr><td class="cell-main"><div class="cell-flat">{esc(a.flat)}</div>'
        f'<div class="cell-owner">{esc(a.owner or "Unnamed owner")}</div></td>'
        f'<td class="cell-date">{a.end_date.strftime("%d %b %Y")}</td>'
        f'<td class="cell-count"><div class="countdown">'
        f'<div class="meter" aria-hidden="true"><i style="width:{pct:.0f}%"></i></div>'
        f'<span class="count-num">{a.days_left}d</span></div></td></tr>'
    )


def render_kpi(icon: str, label: str, value: int, sub: str, cls: str = "") -> str:
    return (f'<div class="stat {cls}"><span class="stat-icon">{icon}</span>'
            f'<div class="label">{label}</div>'
            f'<div class="value" data-countup="{value}">0</div>'
            f'<div class="stat-sub">{sub}</div></div>')


# ─────────────────────────────────────────────────────────────────────────────
# Page template. Uses %%TOKEN%% placeholders (NOT str.format), so CSS/JS braces
# can be written normally. Never write two consecutive percent signs in CSS/JS.
# ─────────────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#05070c">
<title>Renewal reminders &ndash; %%GENERATED_DATE%%</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:24px; --sp-6:32px; --sp-7:48px;
    --fs-xs:11px; --fs-sm:13px; --fs-md:16px; --fs-lg:22px; --fs-xl:27px;
    --ease:cubic-bezier(0.16,1,0.3,1);
    --bg:#05070c; --surface:rgba(19,24,38,0.72); --surface-2:rgba(255,255,255,0.03);
    --border:rgba(255,255,255,0.08); --border-hi:rgba(255,255,255,0.16);
    --text:#edf0f5; --text-dim:#9aa5b8;
    --accent:#22c55e; --accent-glow:#00f090;
    --primary:#7c9bff; --danger:#ff6b85; --amber:#ffc14d; --critical:#ff5577;
    --shadow:inset 0 1px 0 rgba(255,255,255,0.06), 0 1px 2px rgba(0,0,0,0.30), 0 8px 24px -8px rgba(0,0,0,0.45);
    --mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    --sans:'Space Grotesk',-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  }
  *,*::before,*::after { box-sizing:border-box; margin:0; padding:0; }
  [hidden] { display:none !important; }
  ::selection { background:rgba(34,197,94,0.30); color:#eafff2; }
  ::-webkit-scrollbar { width:8px; height:8px; }
  ::-webkit-scrollbar-track { background:transparent; }
  ::-webkit-scrollbar-thumb { background:rgba(255,255,255,0.10); border-radius:999px; border:2px solid var(--bg); }
  ::-webkit-scrollbar-thumb:hover { background:rgba(255,255,255,0.18); }
  html { scrollbar-width:thin; scrollbar-color:rgba(255,255,255,0.12) transparent; -webkit-text-size-adjust:100%; }

  body {
    position:relative; background:var(--bg); color:var(--text); font-family:var(--sans);
    font-size:var(--fs-sm); line-height:1.5; min-height:100vh; min-height:100dvh;
    padding:var(--sp-6) var(--sp-4) var(--sp-7); -webkit-font-smoothing:antialiased;
  }
  /* Grid + glow live on fixed pseudo-elements (cheaper than background-attachment:fixed) */
  body::before {
    content:""; position:fixed; inset:0; z-index:0; pointer-events:none;
    background-image:
      linear-gradient(rgba(255,255,255,0.022) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255,255,255,0.022) 1px, transparent 1px),
      radial-gradient(ellipse 900px 400px at 50% -10%, rgba(109,139,255,0.10), transparent 60%);
    background-size:42px 42px, 42px 42px, 100% 100%;
  }
  body::after {
    content:""; position:fixed; inset:0; z-index:0; pointer-events:none;
    background:radial-gradient(620px 420px at 6% 104%, rgba(34,197,94,0.075), transparent 62%),
               radial-gradient(520px 400px at 100% 18%, rgba(99,102,241,0.09), transparent 62%);
  }
  .accent-bar { position:fixed; top:0; left:0; right:0; height:2px; z-index:999;
    background:linear-gradient(90deg,#00f5ff 0%,#22c55e 38%,#a855f7 70%,#ff6b85 100%); }
  .grain { position:fixed; inset:0; width:100%; height:100%; z-index:0; pointer-events:none;
    opacity:0.035; mix-blend-mode:overlay; }
  @media (max-width:700px) { .grain { display:none; } }

  .page { position:relative; z-index:1; max-width:1180px; margin:0 auto; }
  a, button { -webkit-tap-highlight-color:transparent; }
  :focus-visible { outline:2px solid var(--primary); outline-offset:2px; border-radius:6px; }

  /* Banners */
  .banner { display:flex; align-items:center; gap:var(--sp-3); border-radius:10px;
    padding:var(--sp-3) var(--sp-4); font-size:var(--fs-sm); margin-bottom:var(--sp-4); }
  .banner button { margin-left:auto; flex-shrink:0; display:grid; place-items:center; width:28px; height:28px;
    border-radius:50%; background:rgba(255,255,255,0.08); border:1px solid transparent; color:inherit; cursor:pointer; }
  .banner button:hover { background:rgba(255,255,255,0.16); border-color:var(--border-hi); }
  .banner.stale { background:rgba(255,85,119,0.10); border:1px solid rgba(255,85,119,0.40); color:var(--critical); font-weight:600; }
  .banner.demo { background:rgba(124,155,255,0.10); border:1px solid rgba(124,155,255,0.35); color:var(--primary); }

  /* Header */
  .top { display:flex; justify-content:space-between; align-items:center; gap:var(--sp-3);
    flex-wrap:wrap; margin-bottom:var(--sp-5); }
  .brand { display:flex; align-items:center; gap:var(--sp-3); min-width:0; }
  .logo { width:48px; height:48px; border-radius:13px; flex-shrink:0; display:grid; place-items:center;
    background:linear-gradient(135deg,#22c55e,#6366f1); color:#fff; font-weight:700; font-size:var(--fs-md);
    letter-spacing:-0.02em; box-shadow:0 0 28px rgba(34,197,94,0.4), 0 0 60px rgba(99,102,241,0.2); }
  h1 { font-size:var(--fs-lg); font-weight:700; letter-spacing:-0.02em; line-height:1.2; }
  .subtitle { font-size:var(--fs-sm); color:var(--text-dim); margin-top:2px; }
  .sync-badge { display:inline-flex; align-items:center; gap:var(--sp-2); font-family:var(--mono);
    font-size:var(--fs-xs); color:var(--accent); background:rgba(34,197,94,0.08);
    border:1px solid rgba(34,197,94,0.28); padding:6px var(--sp-4) 6px var(--sp-3); border-radius:999px; }
  .sync-dot { width:7px; height:7px; border-radius:50%; background:var(--accent); box-shadow:0 0 8px var(--accent-glow); }

  /* KPI cards: three evenly spaced columns */
  .stats { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:var(--sp-3); margin-bottom:var(--sp-4); }
  .stat { position:relative; overflow:hidden; border-radius:14px; padding:var(--sp-4) var(--sp-4) var(--sp-4) var(--sp-5);
    min-height:112px; display:flex; flex-direction:column; justify-content:flex-end;
    background:var(--surface); backdrop-filter:blur(12px); -webkit-backdrop-filter:blur(12px);
    border:1px solid var(--border); box-shadow:var(--shadow); }
  .stat::after { content:""; position:absolute; inset:0; pointer-events:none;
    background:radial-gradient(ellipse 90% 70% at 0% 110%, var(--tint, rgba(124,155,255,0.06)), transparent 60%); }
  .stat > * { position:relative; z-index:1; }
  .stat.warn { --tint:rgba(255,77,109,0.09); border-left:3px solid var(--danger); }
  .stat.crit { --tint:rgba(255,45,85,0.10); border-left:3px solid var(--critical); }
  .stat.ok { --tint:rgba(34,197,94,0.08); border-left:3px solid var(--accent); }
  .stat-icon { position:absolute; top:var(--sp-4); right:var(--sp-4); z-index:1; color:var(--text-dim); opacity:0.7; }
  .stat .label { font-size:var(--fs-sm); color:var(--text-dim); font-weight:500; }
  .stat .value { font-family:var(--mono); font-size:var(--fs-xl); font-weight:600; line-height:1.15;
    letter-spacing:-0.03em; font-variant-numeric:tabular-nums; margin-top:var(--sp-1); }
  .stat-sub { font-size:var(--fs-xs); color:var(--text-dim); margin-top:2px; }
  .stat.warn .value { color:var(--danger); text-shadow:0 0 20px rgba(255,77,109,0.50); }
  .stat.crit .value { color:var(--critical); text-shadow:0 0 20px rgba(255,45,85,0.50); }
  .stat.ok .value { color:var(--accent); text-shadow:0 0 20px rgba(34,197,94,0.40); }

  /* Urgency bar: the queue's shape at a glance */
  .sevbar { display:flex; gap:3px; height:6px; margin-bottom:var(--sp-5); }
  .sevbar span { flex-basis:0; min-width:8px; border-radius:999px; animation:sweep .6s var(--ease) both; }
  .sevbar span:nth-child(2) { animation-delay:60ms; } .sevbar span:nth-child(3) { animation-delay:120ms; }
  .sevbar span:nth-child(4) { animation-delay:180ms; }
  @keyframes sweep { from { opacity:0; transform:scaleX(0); transform-origin:left; } }
  .sevbar .overdue { background:var(--critical); box-shadow:0 0 10px rgba(255,45,85,0.55); }
  .sevbar .today { background:var(--danger); box-shadow:0 0 10px rgba(255,77,109,0.50); }
  .sevbar .soon { background:var(--amber); box-shadow:0 0 10px rgba(255,176,32,0.45); }
  .sevbar .window30 { background:var(--primary); opacity:0.7; }

  /* Section headings */
  h2 { font-size:var(--fs-md); font-weight:600; letter-spacing:-0.01em; margin:var(--sp-6) 0 var(--sp-3);
    display:flex; justify-content:space-between; align-items:baseline; gap:var(--sp-3);
    border-left:3px solid var(--primary); padding-left:10px; }
  h2 .count { font-size:var(--fs-sm); font-weight:400; color:var(--text-dim); }

  /* Toolbar (sticky so search/filters stay reachable while scrolling) */
  .toolbar-wrap { position:sticky; top:0; z-index:30; padding:var(--sp-2) 0 var(--sp-3);
    background:linear-gradient(180deg,var(--bg) 75%,transparent); }
  .toolbar { display:flex; gap:var(--sp-3); flex-wrap:wrap; align-items:center; }
  .search-wrap { flex:1 1 240px; position:relative; }
  input[type="search"]::-webkit-search-cancel-button,
  input[type="search"]::-webkit-search-decoration { -webkit-appearance:none; appearance:none; }
  .search-input { width:100%; height:44px; padding:0 72px 0 40px; color:var(--text); font-family:var(--sans);
    font-size:var(--fs-sm); background:var(--surface); border:1px solid var(--border); border-radius:10px;
    box-shadow:var(--shadow); transition:border-color .2s var(--ease), box-shadow .2s var(--ease); }
  .search-input::placeholder { color:var(--text-dim); }
  .search-input:focus { outline:none; border-color:rgba(124,155,255,0.6);
    box-shadow:0 0 0 3px rgba(124,155,255,0.12), 0 0 20px rgba(124,155,255,0.08); }
  .search-icon { position:absolute; left:13px; top:50%; transform:translateY(-50%); color:var(--text-dim);
    pointer-events:none; display:flex; }
  .search-clear { position:absolute; right:40px; top:50%; transform:translateY(-50%); width:24px; height:24px;
    display:grid; place-items:center; border-radius:50%; background:rgba(255,255,255,0.08);
    border:1px solid var(--border); color:var(--text-dim); cursor:pointer; }
  .search-clear:hover { color:var(--text); border-color:var(--border-hi); }
  .search-kbd { position:absolute; right:var(--sp-2); top:50%; transform:translateY(-50%); pointer-events:none; }
  kbd { font-family:var(--mono); font-size:10px; color:var(--text-dim); background:rgba(255,255,255,0.06);
    border:1px solid var(--border-hi); border-bottom-width:2px; border-radius:5px; padding:2px var(--sp-2); }
  @media (hover:none) { .search-kbd { display:none; } }
  .chips { display:flex; gap:var(--sp-2); flex-wrap:wrap; }
  .chip { display:inline-flex; align-items:center; gap:6px; min-height:36px; padding:0 var(--sp-4);
    font-family:var(--sans); font-size:var(--fs-sm); font-weight:500; color:var(--text-dim); cursor:pointer;
    background:var(--surface); border:1px solid var(--border); border-radius:999px; transition:all .2s var(--ease); }
  .chip .n { font-family:var(--mono); font-size:var(--fs-xs); color:var(--text-dim); }
  @media (hover:hover) and (pointer:fine) { .chip:hover { border-color:var(--border-hi); color:var(--text); } }
  .chip[aria-pressed="true"] { color:var(--text); border-color:rgba(124,155,255,0.65);
    background:rgba(124,155,255,0.14); box-shadow:0 0 12px rgba(124,155,255,0.25); }
  .chip[data-filter="overdue"][aria-pressed="true"] { color:var(--critical); border-color:rgba(255,45,85,0.55); background:rgba(255,45,85,0.12); box-shadow:0 0 12px rgba(255,45,85,0.22); }
  .chip[data-filter="today"][aria-pressed="true"] { color:var(--danger); border-color:rgba(255,77,109,0.55); background:rgba(255,77,109,0.12); box-shadow:0 0 12px rgba(255,77,109,0.22); }
  .chip[data-filter="soon"][aria-pressed="true"] { color:var(--amber); border-color:rgba(255,176,32,0.55); background:rgba(255,176,32,0.12); box-shadow:0 0 12px rgba(255,176,32,0.22); }

  /* Owner cards */
  .owner-grid { display:grid; gap:var(--sp-3); align-items:start;
    grid-template-columns:repeat(auto-fill,minmax(min(100%,420px),1fr)); }
  .owner-card { position:relative; overflow:hidden; border-radius:16px; padding:var(--sp-4);
    background:var(--surface); backdrop-filter:blur(12px); -webkit-backdrop-filter:blur(12px);
    border:1px solid var(--border); box-shadow:var(--shadow);
    animation:card-in .45s var(--ease) both; animation-delay:calc(var(--i,0) * 60ms); }
  @keyframes card-in { from { opacity:0; transform:translateY(12px) scale(0.98); } }
  .owner-card[data-worst="overdue"] { box-shadow:var(--shadow), 0 0 0 1px rgba(255,45,85,0.14), 0 8px 32px rgba(255,45,85,0.10); }
  .owner-card[data-worst="today"] { box-shadow:var(--shadow), 0 0 0 1px rgba(255,77,109,0.14), 0 8px 32px rgba(255,77,109,0.10); }
  .owner-card[data-worst="soon"] { box-shadow:var(--shadow), 0 0 0 1px rgba(255,176,32,0.12), 0 8px 32px rgba(255,176,32,0.07); }
  .owner-card::before { content:""; position:absolute; inset:0; pointer-events:none; opacity:0;
    background:radial-gradient(260px circle at var(--mx,50%) var(--my,50%), rgba(124,155,255,0.09), transparent 70%);
    transition:opacity .3s var(--ease); }
  .owner-card > * { position:relative; }
  @media (hover:hover) and (pointer:fine) {
    .owner-card { transition:border-color .2s var(--ease); }
    .owner-card:hover { border-color:rgba(124,155,255,0.40); }
    .owner-card:hover::before { opacity:1; }
  }
  .owner-head { display:flex; align-items:center; gap:var(--sp-3); margin-bottom:var(--sp-3); }
  .owner-avatar { width:34px; height:34px; min-width:34px; border-radius:50%; display:grid; place-items:center;
    background:linear-gradient(135deg,#6366f1,#22c55e); color:#fff; font-weight:700; font-size:var(--fs-sm); }
  .owner-card[data-worst="overdue"] .owner-avatar { background:linear-gradient(135deg,#ff5577,#c91c45); }
  .owner-card[data-worst="today"] .owner-avatar { background:linear-gradient(135deg,#ff6b85,#d63063); }
  .owner-card[data-worst="soon"] .owner-avatar { background:linear-gradient(135deg,#ffc14d,#c87900); color:#2b1a00; }
  .owner-name { flex:1; min-width:0; font-size:var(--fs-md); font-weight:600; overflow-wrap:anywhere; }
  .owner-count { font-size:var(--fs-xs); color:var(--text-dim); background:var(--surface-2);
    border:1px solid var(--border); border-radius:999px; padding:2px 10px; white-space:nowrap; }
  .rows { display:flex; flex-direction:column; gap:var(--sp-2); }

  /* Property rows: borderless, faint translucent fill, crisp 3px severity line on the left edge */
  .property-row { --sev:var(--primary); display:flex; flex-direction:column; gap:var(--sp-3);
    position:relative; overflow:hidden; padding:var(--sp-3) var(--sp-4) var(--sp-4) 20px; border-radius:11px;
    background:rgba(255,255,255,0.03); transition:background .15s var(--ease); }
  .property-row::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background:var(--sev); }
  .property-row[data-sev="overdue"] { --sev:var(--critical); }
  .property-row[data-sev="today"] { --sev:var(--danger); }
  .property-row[data-sev="soon"] { --sev:var(--amber); }
  @media (hover:hover) and (pointer:fine) { .property-row:hover { background:rgba(255,255,255,0.05); } }
  .row-top { display:flex; justify-content:space-between; align-items:flex-start; gap:var(--sp-3); }
  .row-main { min-width:0; }
  .flat-name { font-size:var(--fs-sm); font-weight:600; line-height:1.4; overflow-wrap:anywhere; }
  .tenant { font-size:var(--fs-xs); color:var(--text-dim); margin-top:2px; }
  .days { text-align:right; flex-shrink:0; line-height:1; }
  .days-num { font-family:var(--mono); font-size:var(--fs-lg); font-weight:600; color:var(--sev); letter-spacing:-0.03em; }
  .property-row:not([data-sev="window30"]) .days-num { text-shadow:0 0 18px color-mix(in srgb, var(--sev) 55%, transparent); }
  .days small { display:block; font-size:var(--fs-xs); color:var(--text-dim); margin-top:3px; }
  .row-meta { display:flex; align-items:center; gap:var(--sp-3); flex-wrap:wrap; }
  .end-date { font-family:var(--mono); font-size:var(--fs-xs); color:var(--text-dim); }

  .pill { font-family:var(--mono); font-size:var(--fs-xs); font-weight:500; padding:3px 10px;
    border-radius:999px; white-space:nowrap; }
  .pill.window30 { background:rgba(124,155,255,0.12); color:var(--primary); border:1px solid rgba(124,155,255,0.25); }
  .pill.soon { background:rgba(255,176,32,0.11); color:var(--amber); border:1px solid rgba(255,176,32,0.30); box-shadow:0 0 10px rgba(255,176,32,0.25); }
  .pill.today { background:rgba(255,77,109,0.11); color:var(--danger); border:1px solid rgba(255,77,109,0.35); box-shadow:0 0 10px rgba(255,77,109,0.30); }
  .pill.overdue { background:rgba(255,45,85,0.14); color:var(--critical); border:1px solid rgba(255,45,85,0.40); box-shadow:0 0 12px rgba(255,45,85,0.35); }

  /* Actions: quiet copy button + ghost WhatsApp button that fills on hover or press */
  .row-actions { display:flex; gap:var(--sp-2); align-items:stretch; }
  .icon-btn { width:44px; min-height:44px; display:grid; place-items:center; cursor:pointer; color:var(--text-dim);
    background:transparent; border:1px solid var(--border); border-radius:9px; transition:all .15s var(--ease); }
  .icon-btn:hover { color:var(--text); border-color:var(--border-hi); background:rgba(255,255,255,0.04); }
  .send-btn { flex:1; min-height:44px; display:inline-flex; align-items:center; justify-content:center; gap:7px;
    padding:0 var(--sp-4); border-radius:9px; background:rgba(34,197,94,0.06); color:var(--accent);
    border:1px solid rgba(34,197,94,0.35); font-weight:600; font-size:var(--fs-sm); text-decoration:none;
    white-space:nowrap; letter-spacing:0.01em;
    transition:background .18s var(--ease), color .18s var(--ease), border-color .18s var(--ease), box-shadow .18s var(--ease), transform .15s var(--ease); }
  .send-btn:not(.disabled):active, .send-btn:not(.disabled):focus-visible { background:var(--accent); color:#04150b; border-color:var(--accent); }
  .send-btn:not(.disabled):active { transform:scale(0.985); }
  .send-btn.disabled { background:transparent; color:var(--critical); border:1px dashed rgba(255,85,119,0.55);
    font-weight:500; font-size:var(--fs-xs); white-space:normal; text-align:center; line-height:1.3; }
  @media (hover:hover) and (pointer:fine) {
    .icon-btn, .send-btn { min-height:38px; }
    .icon-btn { width:38px; }
    .send-btn:not(.disabled):hover { background:var(--accent); color:#04150b; border-color:var(--accent);
      box-shadow:0 6px 22px rgba(34,197,94,0.40); }
  }

  /* Upcoming renewals: data grid */
  .table-wrap { border-radius:16px; overflow-x:auto; background:var(--surface);
    backdrop-filter:blur(12px); -webkit-backdrop-filter:blur(12px);
    border:1px solid var(--border); box-shadow:var(--shadow); }
  table { width:100%; border-collapse:collapse; }
  th { text-align:left; font-size:var(--fs-xs); font-weight:500; color:var(--text-dim);
    padding:var(--sp-3) var(--sp-5); border-bottom:1px solid var(--border); background:rgba(255,255,255,0.02); }
  th:last-child { text-align:right; }
  td { padding:14px var(--sp-5); vertical-align:middle; border-bottom:1px solid rgba(255,255,255,0.05); }
  tbody tr:last-child td { border-bottom:none; }
  tbody tr { transition:background .15s var(--ease); }
  tbody tr:hover { background:rgba(124,155,255,0.05); }
  tbody tr:hover td:first-child { box-shadow:inset 3px 0 0 var(--primary); }
  .cell-flat { font-size:var(--fs-sm); font-weight:600; line-height:1.4; }
  .cell-owner { font-size:var(--fs-xs); color:var(--text-dim); margin-top:2px; }
  .cell-date { font-family:var(--mono); font-size:var(--fs-xs); color:var(--text-dim); white-space:nowrap; }
  .cell-count { width:200px; }
  .countdown { display:flex; align-items:center; gap:var(--sp-3); }
  .meter { flex:1; min-width:56px; height:4px; border-radius:999px; background:rgba(255,255,255,0.07); overflow:hidden; }
  .meter i { display:block; height:100%; border-radius:inherit; background:linear-gradient(90deg,var(--primary),#b4c3ff); }
  .count-num { font-family:var(--mono); font-size:var(--fs-sm); font-weight:600; min-width:34px; text-align:right;
    font-variant-numeric:tabular-nums; }
  .grid-note { color:var(--text-dim); font-size:var(--fs-sm); }

  .empty { text-align:center; padding:var(--sp-7) var(--sp-5); font-size:var(--fs-sm); color:var(--text-dim);
    border-radius:14px; background:var(--surface); border:1px solid var(--border); }
  .empty strong { display:block; color:var(--text); font-size:var(--fs-md); margin-bottom:var(--sp-1); }

  footer { margin-top:var(--sp-6); padding-top:var(--sp-5); border-top:1px solid var(--border);
    font-size:var(--fs-xs); color:var(--text-dim); line-height:1.7; text-align:center; }

  .toast { position:fixed; left:50%; bottom:calc(var(--sp-5) + env(safe-area-inset-bottom, 0px));
    transform:translate(-50%,16px); opacity:0; pointer-events:none; z-index:1000;
    background:#101522; border:1px solid var(--border-hi); color:var(--text); border-radius:18px;
    padding:var(--sp-2) var(--sp-5); font-size:var(--fs-sm); font-weight:500; text-align:center;
    max-width:calc(100vw - 32px); box-shadow:0 8px 32px rgba(0,0,0,0.5);
    transition:opacity .25s var(--ease), transform .25s var(--ease); }
  .toast.show { opacity:1; transform:translate(-50%,0); }
  .toast.success { border-color:rgba(34,197,94,0.55); color:var(--accent); }
  .toast.error { border-color:rgba(255,77,109,0.45); color:var(--danger); }

  @media (max-width:560px) {
    body { padding-left:var(--sp-3); padding-right:var(--sp-3); }
    .stats { gap:var(--sp-2); }
    .stat { min-height:92px; padding:var(--sp-3) var(--sp-3) var(--sp-3) var(--sp-4); }
    .stat-icon, .stat-sub { display:none; }
    .stat .label { font-size:var(--fs-xs); }
    .stat .value { font-size:var(--fs-lg); }
    .chips { flex-wrap:nowrap; overflow-x:auto; padding-bottom:4px; margin:0 calc(var(--sp-3) * -1);
      padding-left:var(--sp-3); padding-right:var(--sp-3); }
    .chip { flex-shrink:0; }
    th:nth-child(2), td.cell-date { display:none; }
    th, td { padding-left:var(--sp-4); padding-right:var(--sp-4); }
    .cell-count { width:150px; }
  }
  @media (prefers-reduced-motion:reduce) {
    *,*::before,*::after { animation:none !important; transition:none !important; scroll-behavior:auto !important; }
  }
</style>
</head>
<body data-generated="%%GENERATED_ISO%%">
<div class="accent-bar"></div>
<svg class="grain" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <filter id="g"><feTurbulence type="fractalNoise" baseFrequency="0.85" numOctaves="2" stitchTiles="stitch"/>
  <feColorMatrix type="saturate" values="0"/></filter>
  <rect width="100%" height="100%" filter="url(#g)"/>
</svg>

<main class="page">
  %%DEMO_BANNER%%
  <div id="stale" class="banner stale" role="alert" hidden>
    This page is more than %%STALE_HOURS%% hours old. The daily update may have failed &ndash; check the GitHub Action before sending anything.
    <button type="button" onclick="this.parentElement.hidden=true" aria-label="Dismiss">%%ICON_CLOSE%%</button>
  </div>

  <div class="top">
    <div class="brand">
      <div class="logo" aria-hidden="true">RR</div>
      <div>
        <h1>Renewal reminders</h1>
        <div class="subtitle">%%GENERATED_DATE%% &middot; %%TRACKED%%</div>
      </div>
    </div>
    <div class="sync-badge"><span class="sync-dot"></span>Synced %%SYNC%%</div>
  </div>

  <section class="stats" aria-label="Summary">%%STATS%%</section>
  %%SEVBAR%%

  <h2>Action queue <span class="count" id="resultCount" aria-live="polite">%%GROUP_COUNT%%</span></h2>

  <div class="toolbar-wrap">
    <div class="toolbar">
      <div class="search-wrap">
        <span class="search-icon">%%ICON_SEARCH%%</span>
        <input type="search" id="queueSearch" class="search-input" aria-label="Search owner, property or tenant"
               placeholder="Search owner, property or tenant" autocomplete="off" spellcheck="false">
        <button type="button" id="searchClear" class="search-clear" aria-label="Clear search" hidden>%%ICON_CLOSE%%</button>
        <span class="search-kbd"><kbd>/</kbd></span>
      </div>
      <div class="chips" id="filterChips" role="group" aria-label="Filter by status">%%CHIPS%%</div>
    </div>
  </div>

  <div id="queueEmpty" class="empty" role="status" hidden>
    <strong>No matches</strong>Try a different search or clear the filter.
  </div>
  <div id="cards">%%CARDS%%</div>

  <h2>Upcoming renewals <span class="count">next %%UPCOMING_WINDOW%% days</span></h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th scope="col">Property</th><th scope="col">Ends</th><th scope="col">Time until renewal</th></tr></thead>
      <tbody>%%UPCOMING%%</tbody>
    </table>
  </div>

  <footer>Send opens a pre-filled WhatsApp message from your own number. Nothing sends automatically.</footer>
</main>

<div id="toast" class="toast" role="status" aria-live="polite"></div>

<script>
(function () {
  'use strict';
  var $  = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var fine   = window.matchMedia && window.matchMedia('(hover: hover) and (pointer: fine)').matches;

  /* Stale-data warning */
  var gen = new Date(document.body.getAttribute('data-generated'));
  if (!isNaN(gen) && Date.now() - gen > %%STALE_HOURS%% * 3600 * 1000) { $('#stale').hidden = false; }

  /* Count-up on KPI numbers */
  $$('[data-countup]').forEach(function (el) {
    var target = parseInt(el.getAttribute('data-countup'), 10) || 0;
    if (reduce || target === 0) { el.textContent = target; return; }
    var t0 = null;
    function tick(ts) {
      if (t0 === null) t0 = ts;
      var p = Math.min((ts - t0) / 650, 1);
      el.textContent = Math.round((1 - Math.pow(1 - p, 3)) * target);
      if (p < 1) requestAnimationFrame(tick); else el.textContent = target;
    }
    requestAnimationFrame(tick);
  });

  /* Cursor spotlight (desktop pointer only) */
  if (fine && !reduce) {
    $$('.owner-card').forEach(function (card) {
      card.addEventListener('mousemove', function (e) {
        var r = card.getBoundingClientRect();
        card.style.setProperty('--mx', (e.clientX - r.left) + 'px');
        card.style.setProperty('--my', (e.clientY - r.top) + 'px');
      });
    });
  }

  /* Search + filter (row level, so mixed-severity owners behave) */
  var rows = $$('.property-row'), cards = $$('.owner-card');
  var inp = $('#queueSearch'), clearBtn = $('#searchClear'), empty = $('#queueEmpty');
  var countEl = $('#resultCount'), groupText = countEl ? countEl.textContent : '', filt = 'all';

  function run() {
    var q = (inp.value || '').trim().toLowerCase(), shown = 0;
    rows.forEach(function (row) {
      var okQ = !q || (row.getAttribute('data-search') || '').indexOf(q) !== -1;
      var okF = filt === 'all' || row.getAttribute('data-sev') === filt;
      row.hidden = !(okQ && okF);
      if (okQ && okF) shown++;
    });
    cards.forEach(function (card) {
      card.hidden = $$('.property-row:not([hidden])', card).length === 0;
    });
    empty.hidden = !(rows.length > 0 && shown === 0);
    if (countEl && rows.length) {
      countEl.textContent = (shown === rows.length) ? groupText : shown + ' of ' + rows.length + ' properties';
    }
    if (clearBtn) clearBtn.hidden = !inp.value;
  }
  inp.addEventListener('input', run);
  if (clearBtn) clearBtn.addEventListener('click', function () { inp.value = ''; run(); inp.focus(); });
  $$('#filterChips .chip').forEach(function (chip) {
    chip.addEventListener('click', function () {
      $$('#filterChips .chip').forEach(function (c) { c.setAttribute('aria-pressed', 'false'); });
      chip.setAttribute('aria-pressed', 'true');
      filt = chip.getAttribute('data-filter');
      run();
    });
  });
  run();

  /* Toast + copy message */
  var toastEl = $('#toast'), toastT;
  function toast(msg, type) {
    toastEl.textContent = msg;
    toastEl.className = 'toast' + (type ? ' ' + type : '');
    toastEl.classList.add('show');
    clearTimeout(toastT);
    toastT = setTimeout(function () { toastEl.classList.remove('show'); }, 2000);
  }
  function copyText(t) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(t);
    return new Promise(function (res, rej) {
      var ta = document.createElement('textarea');
      ta.value = t; ta.setAttribute('readonly', ''); ta.style.cssText = 'position:fixed;opacity:0';
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy') ? res() : rej(); } catch (e) { rej(e); }
      document.body.removeChild(ta);
    });
  }
  document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('.copy-btn') : null;
    if (!btn) return;
    var row = btn.closest('.property-row');
    copyText(row.getAttribute('data-msg') || '').then(
      function () { toast('Message copied', 'success'); },
      function () { toast('Copy failed \u2013 select the text manually', 'error'); });
  });

  /* Keyboard: "/" focuses search, Esc clears it */
  document.addEventListener('keydown', function (e) {
    var tag = (document.activeElement && document.activeElement.tagName) || '';
    if (e.key === '/' && tag !== 'INPUT' && tag !== 'TEXTAREA') { e.preventDefault(); inp.focus(); }
    else if (e.key === 'Escape' && document.activeElement === inp) { inp.value = ''; run(); inp.blur(); }
  });
})();
</script>
</body>
</html>"""


def fill(template: str, **tokens) -> str:
    """Single pass, so inserted data can never be re-scanned for tokens."""
    def repl(m):
        try:
            return str(tokens[m.group(1)])
        except KeyError:
            raise RuntimeError(f"Unfilled template token: %%{m.group(1)}%%") from None
    return re.sub(r"%%([A-Z_]+)%%", repl, template)


# ─────────────────────────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────────────────────────

def build_dashboard(agreements: "list[Agreement]", now: datetime, demo: bool) -> "tuple[str, dict]":
    active = [a for a in agreements if ACTIVE_WINDOW_MIN_DAYS <= a.days_left <= ACTIVE_WINDOW_MAX_DAYS]
    upcoming = sorted((a for a in agreements
                       if ACTIVE_WINDOW_MAX_DAYS < a.days_left <= UPCOMING_WINDOW_DAYS),
                      key=lambda a: a.days_left)

    tier_counts = {t: 0 for t in TIER_ORDER}
    for a in active:
        tier_counts[classify_badge(a.days_left)[0]] += 1
    urgent = tier_counts["overdue"] + tier_counts["today"]

    # Group by owner (case-insensitive) + phone; worst property first, cards worst-first
    groups: dict = defaultdict(list)
    for a in active:
        groups[(a.owner.strip().lower(), a.phone or a.phone_raw)].append(a)
    ordered = sorted(groups.values(),
                     key=lambda rows: -max(severity(r.days_left) for r in rows))
    cards_html = "".join(render_card(rows[0].owner, rows, i) for i, rows in enumerate(ordered))
    if cards_html:
        cards_html = f'<div class="owner-grid">{cards_html}</div>'
    else:
        cards_html = ('<div class="empty"><strong>All clear</strong>'
                      'Nothing needs a reminder right now.</div>')

    stats = "".join([
        render_kpi(ICON_BOLT, "Needs action", len(active), "In the action window",
                   "warn" if active else "ok"),
        render_kpi(ICON_ALERT, "Urgent", urgent, "Overdue or expiring today",
                   "crit" if urgent else "ok"),
        render_kpi(ICON_CAL, "Coming up", len(upcoming), f"Next {UPCOMING_WINDOW_DAYS} days"),
    ])

    sevbar = ""
    if active:
        segs = "".join(f'<span class="{t}" style="flex-grow:{tier_counts[t]}" '
                       f'title="{TIER_NAMES[t]}: {tier_counts[t]}"></span>'
                       for t in TIER_ORDER if tier_counts[t])
        summary = ", ".join(f"{tier_counts[t]} {TIER_NAMES[t].lower()}"
                            for t in TIER_ORDER if tier_counts[t])
        sevbar = f'<div class="sevbar" role="img" aria-label="Queue by urgency: {summary}">{segs}</div>'

    chips = [f'<button type="button" class="chip" data-filter="all" aria-pressed="true">All '
             f'<span class="n">{len(active)}</span></button>']
    for t in TIER_ORDER:
        chips.append(f'<button type="button" class="chip" data-filter="{t}" aria-pressed="false">'
                     f'{TIER_NAMES[t]} <span class="n">{tier_counts[t]}</span></button>')

    if upcoming:
        up_rows = "".join(render_upcoming_row(a) for a in upcoming[:UPCOMING_PREVIEW_LIMIT])
        if len(upcoming) > UPCOMING_PREVIEW_LIMIT:
            up_rows += (f'<tr><td colspan="3" class="grid-note">+ {len(upcoming) - UPCOMING_PREVIEW_LIMIT} '
                        f'more in the next {UPCOMING_WINDOW_DAYS} days (see the sheet)</td></tr>')
    else:
        up_rows = (f'<tr><td colspan="3" class="grid-note">No renewals in the next '
                   f'{UPCOMING_WINDOW_DAYS} days.</td></tr>')

    n_owners = len(ordered)
    group_count = (f"{n_owners} owner{'s' if n_owners != 1 else ''}, "
                   f"{len(active)} propert{'y' if len(active) == 1 else 'ies'}") if active else ""

    demo_banner = ('<div class="banner demo" role="note">Demo data. These are sample agreements, '
                   'not real ones.</div>') if demo else ""

    page = fill(
        HTML_TEMPLATE,
        GENERATED_DATE=now.strftime("%d %b %Y"),
        GENERATED_ISO=now.isoformat(timespec="seconds"),
        SYNC=esc(now.strftime("%d %b, %I:%M %p IST")),
        TRACKED=f"{len(agreements)} agreement{'s' if len(agreements) != 1 else ''} tracked",
        STATS=stats, SEVBAR=sevbar, CHIPS="".join(chips),
        GROUP_COUNT=group_count, CARDS=cards_html, UPCOMING=up_rows,
        UPCOMING_WINDOW=UPCOMING_WINDOW_DAYS, STALE_HOURS=STALE_AFTER_HOURS,
        DEMO_BANNER=demo_banner, ICON_SEARCH=ICON_SEARCH, ICON_CLOSE=ICON_CLOSE,
    )
    return page, dict(active=len(active), owners=n_owners, upcoming=len(upcoming),
                      total=len(agreements))


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate the rental renewal dashboard.")
    ap.add_argument("--demo", action="store_true", help="use built-in sample data (no Sheets access)")
    ap.add_argument("--empty", action="store_true", help="with --demo: preview the empty state")
    ap.add_argument("--out", help="output file path (default: docs/index.html, or demo.html)")
    args = ap.parse_args()

    now = datetime.now(IST)
    today = now.date()
    log_ws = None

    if args.demo:
        agreements = demo_agreements(today, empty=args.empty)
        out_path = args.out or DEMO_OUTPUT_FILE
    else:
        fail_fast_on_missing_env()
        spreadsheet = get_spreadsheet()
        log_ws = get_or_create_log_tab(spreadsheet)
        values = with_retry(lambda: spreadsheet.sheet1.get_all_values())
        agreements = parse_agreements(rows_from_values(values), today)
        out_path = args.out or OUTPUT_FILE

    for a in agreements:
        if ACTIVE_WINDOW_MIN_DAYS <= a.days_left <= ACTIVE_WINDOW_MAX_DAYS and not a.phone:
            log(a.owner, a.phone_raw, a.flat, "WARNING",
                "Missing/invalid phone - shown in queue without a Send button")

    page, s = build_dashboard(agreements, now, demo=args.demo)

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)

    summary = (f"active={s['active']} owners={s['owners']} upcoming={s['upcoming']} "
               f"total={s['total']}")
    print(f"Dashboard written to {out_path}: {summary}")

    if log_ws is not None:
        LOG_BUFFER.append([now.strftime("%Y-%m-%d %H:%M:%S"), "", "", "", "RUN", summary])
        try:
            with_retry(lambda: log_ws.append_rows(LOG_BUFFER, value_input_option="RAW"))
        except Exception as e:  # the dashboard is already written; never fail the run over logging
            print(f"Log write failed (dashboard unaffected): {e}", file=sys.stderr)


if __name__ == "__main__":
    main()

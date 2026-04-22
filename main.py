"""
MT5 Price Alert Monitor
-----------------------
Single-file Flask app that:
  - Monitors forex prices using the free Frankfurter API (no key needed)
  - Lets you set price alerts through a built-in web dashboard
  - Calls your phone via Telnyx when a price level is hit

Run:  python main.py
"""

import os
import sqlite3
import threading
import time
import logging
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, Response
import telnyx
import requests as http


# =============================================================================
# Setup
# =============================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# Telnyx credentials — stored as environment variables
TELNYX_API_KEY = os.environ.get("TELNYX_API_KEY", "")
TELNYX_FROM    = os.environ.get("TELNYX_PHONE_NUMBER", "")
ALERT_TO       = os.environ.get("ALERT_PHONE_NUMBER", "")

# Replit exposes your app publicly under this domain
REPLIT_DOMAIN  = os.environ.get("REPLIT_DEV_DOMAIN", "")

DB_PATH         = "alerts.db"
DEFAULT_SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF"]
PRICE_CACHE_TTL = 8   # seconds to cache a fetched price before re-fetching

# In-memory stores
_price_cache: dict[str, tuple[float, float]] = {}  # symbol -> (price, timestamp)
_pending_texml: dict[str, str] = {}                 # token  -> spoken message
_telnyx_connection_id: str | None = None            # cached after first lookup


# =============================================================================
# Database
# =============================================================================

def db():
    """Open a SQLite connection with row-as-dict support."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist yet."""
    with db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS alerts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol       TEXT    NOT NULL,
                condition    TEXT    NOT NULL,       -- 'above' or 'below'
                target       REAL    NOT NULL,
                phone        TEXT    NOT NULL,
                message      TEXT    DEFAULT '',     -- optional custom spoken message
                active       INTEGER DEFAULT 1,
                triggered    INTEGER DEFAULT 0,
                created_at   TEXT    DEFAULT (datetime('now')),
                triggered_at TEXT
            );

            CREATE TABLE IF NOT EXISTS call_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol     TEXT NOT NULL,
                price      REAL NOT NULL,
                phone      TEXT NOT NULL,
                status     TEXT NOT NULL,            -- 'initiated' or 'error'
                error      TEXT,
                call_sid   TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
    log.info("Database ready")


# =============================================================================
# Price fetching  (Frankfurter API — free, no key)
# =============================================================================

def fetch_price(symbol: str) -> float | None:
    """
    Fetch the latest price for a forex pair like EURUSD.
    Results are cached for PRICE_CACHE_TTL seconds to avoid hammering the API.
    Supported pairs: any two ISO 4217 currency codes, e.g. EUR/USD, GBP/JPY.
    """
    symbol = symbol.upper().replace("/", "")
    now = time.time()

    # Return cached value if still fresh
    if symbol in _price_cache:
        price, ts = _price_cache[symbol]
        if now - ts < PRICE_CACHE_TTL:
            return price

    # Expect exactly 6 characters: 3-letter base + 3-letter quote
    if len(symbol) != 6:
        return None
    base, quote = symbol[:3], symbol[3:]

    try:
        r = http.get(
            f"https://api.frankfurter.app/latest?from={base}&to={quote}",
            timeout=6
        )
        r.raise_for_status()
        price = float(r.json()["rates"][quote])
        _price_cache[symbol] = (price, now)
        return price
    except Exception as e:
        log.warning(f"Price fetch failed for {symbol}: {e}")
        return None


# =============================================================================
# Telnyx — connection lookup, TeXML webhook, outbound call
# =============================================================================

def get_telnyx_connection_id() -> str | None:
    """
    Look up the Telnyx connection_id associated with our from-number.
    The connection_id is required to place outbound calls.
    Result is cached so we only hit the API once.
    """
    global _telnyx_connection_id
    if _telnyx_connection_id:
        return _telnyx_connection_id
    if not TELNYX_API_KEY or not TELNYX_FROM:
        return None
    try:
        resp = http.get(
            "https://api.telnyx.com/v2/phone_numbers",
            headers={"Authorization": f"Bearer {TELNYX_API_KEY}"},
            params={"filter[phone_number]": TELNYX_FROM},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if data:
            conn_id = data[0].get("connection_id")
            if conn_id:
                _telnyx_connection_id = str(conn_id)
                log.info(f"Telnyx connection_id resolved: {_telnyx_connection_id}")
                return _telnyx_connection_id
        log.warning(f"Phone number {TELNYX_FROM} not found in Telnyx account")
    except Exception as e:
        log.error(f"Failed to fetch Telnyx connection_id: {e}")
    return None


@app.route("/texml/<token>", methods=["GET", "POST"])
def serve_texml(token):
    """
    Telnyx calls this URL the moment the outbound call is answered.
    We return TeXML (compatible with TwiML) that instructs Telnyx
    to read the alert message aloud twice.
    """
    message = _pending_texml.pop(token, "MT5 price alert triggered.")

    # Escape XML special characters so the message is safe inside TeXML
    safe_msg = (
        message
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )

    texml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'  <Say voice="alice">{safe_msg}</Say>'
        '  <Pause length="1"/>'
        f'  <Say voice="alice">{safe_msg}</Say>'
        "</Response>"
    )
    return Response(texml, mimetype="text/xml")


def make_call(phone: str, symbol: str, price: float,
              condition: str, target: float, custom_msg: str = "") -> dict:
    """
    Place an outbound call via Telnyx.

    Flow:
      1. Build the spoken message.
      2. Store it in _pending_texml under a unique token.
      3. Dial the call, pointing Telnyx at our /texml/<token> webhook.
      4. When the call is answered, Telnyx fetches the TeXML and reads the message.

    Returns a dict with keys: status, call_sid, error.
    """
    spoken_msg = custom_msg or (
        f"MT5 price alert. {symbol} is now {price:.5f}, "
        f"which is {condition} your target of {target:.5f}."
    )

    # Guard: credentials must be set
    if not (TELNYX_API_KEY and TELNYX_FROM):
        log.error("Telnyx credentials not set — skipping call")
        return {"status": "error", "error": "Telnyx not configured", "call_sid": None}

    # Guard: we need the connection_id to place the call
    connection_id = get_telnyx_connection_id()
    if not connection_id:
        return {
            "status": "error",
            "error": (
                f"Could not resolve connection_id for {TELNYX_FROM}. "
                "Check that the number is active in your Telnyx dashboard."
            ),
            "call_sid": None,
        }

    # Store the message so /texml/<token> can serve it when the call connects
    token = f"{phone.replace('+', '')}_{int(time.time())}"
    _pending_texml[token] = spoken_msg

    # Build the public webhook URL that Telnyx will hit on answer
    if REPLIT_DOMAIN:
        webhook_url = f"https://{REPLIT_DOMAIN}/texml/{token}"
    else:
        webhook_url = f"http://localhost:{os.environ.get('PORT', 8000)}/texml/{token}"

    log.info(f"Placing call to {phone} | webhook: {webhook_url}")

    try:
        client = telnyx.Telnyx(api_key=TELNYX_API_KEY)
        result = client.calls.dial(
            connection_id=connection_id,
            to=phone,
            from_=TELNYX_FROM,
            webhook_url=webhook_url,
            time_limit_secs=120,
        )

        # Extract the call control ID from the response
        call_control_id = None
        if hasattr(result, "data") and result.data:
            call_control_id = getattr(result.data, "call_control_id", None)
        if not call_control_id:
            call_control_id = str(result)

        log.info(f"Call placed to {phone} — control_id: {call_control_id}")
        return {"status": "initiated", "call_sid": call_control_id, "error": None}

    except Exception as e:
        _pending_texml.pop(token, None)   # clean up if the dial failed
        log.error(f"Telnyx dial error: {e}")
        return {"status": "error", "error": str(e), "call_sid": None}


# =============================================================================
# Alert monitor  (background thread — checks every 10 seconds)
# =============================================================================

def monitor_loop():
    """Runs forever in a daemon thread, checking alerts every 10 seconds."""
    log.info("Alert monitor started — checking every 10 seconds")
    while True:
        try:
            check_alerts()
        except Exception as e:
            log.error(f"Monitor error: {e}")
        time.sleep(10)


def check_alerts():
    """Fetch active, untriggered alerts and fire calls for any that have hit their target."""
    with db() as c:
        alerts = c.execute(
            "SELECT * FROM alerts WHERE active = 1 AND triggered = 0"
        ).fetchall()

        if not alerts:
            return

        for a in alerts:
            price = fetch_price(a["symbol"])
            if price is None:
                continue

            hit = (
                (a["condition"] == "above" and price >= a["target"]) or
                (a["condition"] == "below" and price <= a["target"])
            )
            if not hit:
                continue

            log.info(f"Alert #{a['id']} triggered: {a['symbol']} @ {price}")

            result = make_call(
                a["phone"], a["symbol"], price,
                a["condition"], a["target"], a["message"]
            )

            now = datetime.utcnow().isoformat()
            c.execute(
                "UPDATE alerts SET triggered = 1, triggered_at = ? WHERE id = ?",
                (now, a["id"])
            )
            c.execute(
                "INSERT INTO call_log (symbol, price, phone, status, error, call_sid) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (a["symbol"], price, a["phone"],
                 result["status"], result["error"], result["call_sid"])
            )
            c.commit()


# =============================================================================
# HTML dashboard  (all markup lives here — no separate template files)
# =============================================================================

DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MT5 Price Alert Monitor</title>
  <style>
    /* ── Reset & base ─────────────────────────────────────────── */
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #0d1117;
      color: #e6edf3;
      min-height: 100vh;
    }

    /* ── Header ───────────────────────────────────────────────── */
    header {
      background: #161b22;
      border-bottom: 1px solid #30363d;
      padding: 14px 28px;
      display: flex;
      align-items: center;
      gap: 10px;
    }
    header h1 { font-size: 17px; font-weight: 700; color: #f0f6fc; }
    .hdot {
      width: 8px; height: 8px; border-radius: 50%;
      display: inline-block; margin-right: 5px;
    }
    .hdot.ok  { background: #3fb950; box-shadow: 0 0 5px #3fb950; }
    .hdot.err { background: #f85149; }
    header .hstatus {
      font-size: 12px; color: #8b949e;
      margin-left: auto; display: flex; align-items: center; gap: 4px;
    }

    /* ── Layout ───────────────────────────────────────────────── */
    .wrap { max-width: 920px; margin: 0 auto; padding: 28px 20px; }

    /* ── Tabs ─────────────────────────────────────────────────── */
    .tabs {
      display: flex; gap: 3px; margin-bottom: 22px;
      background: #161b22; border: 1px solid #30363d;
      border-radius: 9px; padding: 4px; width: fit-content;
    }
    .tab {
      padding: 7px 18px; border-radius: 6px;
      font-size: 13px; font-weight: 500; cursor: pointer;
      color: #8b949e; border: none; background: none; transition: all .15s;
    }
    .tab.active            { background: #238636; color: #fff; }
    .tab:not(.active):hover { color: #e6edf3; }

    /* ── Stat cards ───────────────────────────────────────────── */
    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
      gap: 12px; margin-bottom: 26px;
    }
    .stat {
      background: #161b22; border: 1px solid #30363d;
      border-radius: 10px; padding: 14px;
    }
    .stat .lbl {
      font-size: 11px; color: #8b949e;
      text-transform: uppercase; letter-spacing: .06em; margin-bottom: 5px;
    }
    .stat .val { font-size: 24px; font-weight: 700; }
    .val.blue  { color: #58a6ff; }
    .val.green { color: #3fb950; }
    .val.red   { color: #f85149; }

    /* ── Cards ────────────────────────────────────────────────── */
    .card { background: #161b22; border: 1px solid #30363d; border-radius: 12px; margin-bottom: 20px; }
    .ch {
      padding: 14px 20px; border-bottom: 1px solid #30363d;
      display: flex; align-items: center; justify-content: space-between;
    }
    .ch h2 { font-size: 14px; font-weight: 600; }
    .cb { padding: 18px 20px; }

    /* ── Form elements ────────────────────────────────────────── */
    .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .fg { display: flex; flex-direction: column; gap: 5px; }
    .fg label { font-size: 12px; color: #8b949e; font-weight: 500; }
    .fg.full  { grid-column: 1 / -1; }
    input, select, textarea {
      background: #0d1117; border: 1px solid #30363d; border-radius: 7px;
      padding: 8px 11px; color: #e6edf3; font-size: 13px;
      outline: none; transition: border .15s; font-family: inherit; width: 100%;
    }
    input:focus, select:focus, textarea:focus { border-color: #58a6ff; }
    textarea { resize: vertical; min-height: 56px; }

    /* ── Buttons ──────────────────────────────────────────────── */
    .btn { padding: 8px 16px; border-radius: 7px; border: none; font-size: 13px; font-weight: 600; cursor: pointer; transition: all .15s; }
    .btn-g    { background: #238636; color: #fff; }
    .btn-g:hover { background: #2ea043; }
    .btn-sm   { padding: 4px 10px; font-size: 12px; }
    .btn-gray { background: #21262d; color: #e6edf3; border: 1px solid #30363d; }
    .btn-gray:hover { border-color: #58a6ff; color: #58a6ff; }
    .btn-red  { background: rgba(248,81,73,.1); color: #f85149; border: 1px solid rgba(248,81,73,.15); }
    .btn-red:hover  { background: rgba(248,81,73,.2); }

    /* ── Alert rows ───────────────────────────────────────────── */
    .alert-row {
      display: flex; align-items: center; gap: 12px;
      padding: 13px 0; border-bottom: 1px solid #21262d;
    }
    .alert-row:last-child { border-bottom: none; }
    .sym { font-family: monospace; font-size: 14px; font-weight: 700; color: #58a6ff; min-width: 68px; }
    .ai  { flex: 1; }
    .ai .cond { font-size: 11px; color: #8b949e; }
    .ai .tgt  { font-size: 14px; font-weight: 600; }
    .lp  { font-family: monospace; font-size: 11px; color: #8b949e; min-width: 100px; text-align: right; }
    .badge { padding: 3px 8px; border-radius: 20px; font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; }
    .ba { background: rgba(63,185,80,.12);    color: #3fb950; }
    .bt { background: rgba(88,166,255,.12);   color: #58a6ff; }
    .bp { background: rgba(139,148,158,.1);   color: #8b949e; }
    .acts { display: flex; gap: 5px; }

    /* ── Call log rows ────────────────────────────────────────── */
    .log-row { display: flex; align-items: center; gap: 11px; padding: 11px 0; border-bottom: 1px solid #21262d; font-size: 12px; }
    .log-row:last-child { border-bottom: none; }
    .li { width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
    .li.ok  { background: rgba(63,185,80,.12); color: #3fb950; }
    .li.err { background: rgba(248,81,73,.12); color: #f85149; }
    .lm     { flex: 1; }
    .lm .ls { font-family: monospace; font-weight: 700; color: #58a6ff; }
    .lm .ld { color: #8b949e; font-size: 11px; margin-top: 2px; }
    .lt     { color: #8b949e; font-size: 11px; }

    /* ── Price grid ───────────────────────────────────────────── */
    .pg { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 11px; }
    .pc { background: #0d1117; border: 1px solid #30363d; border-radius: 9px; padding: 13px; }
    .pc .ps { font-family: monospace; font-weight: 700; color: #58a6ff; font-size: 13px; }
    .pc .pp { font-size: 19px; font-weight: 700; margin: 3px 0 2px; }
    .pc .pp.up { color: #3fb950; }
    .pc .pp.dn { color: #f85149; }
    .pc .pu { font-size: 10px; color: #8b949e; }

    /* ── Misc ─────────────────────────────────────────────────── */
    .empty { text-align: center; padding: 36px; color: #8b949e; font-size: 13px; }
    .flash { padding: 9px 13px; border-radius: 7px; font-size: 12px; margin-bottom: 12px; display: none; }
    .flash.ok  { background: rgba(63,185,80,.1); border: 1px solid rgba(63,185,80,.3); color: #3fb950; }
    .flash.err { background: rgba(248,81,73,.1); border: 1px solid rgba(248,81,73,.3); color: #f85149; }
    .phint    { font-size: 12px; color: #58a6ff; margin-top: 4px; }
    .api-note { font-size: 11px; color: #8b949e; margin-top: 14px; }
    .api-note a { color: #58a6ff; }

    /* ── Modal ────────────────────────────────────────────────── */
    .overlay {
      position: fixed; inset: 0; background: rgba(0,0,0,.65);
      display: none; align-items: center; justify-content: center;
      z-index: 100; backdrop-filter: blur(3px);
    }
    .overlay.open { display: flex; }
    .modal { background: #161b22; border: 1px solid #30363d; border-radius: 12px; width: 460px; max-width: 95vw; }
    .mh { display: flex; align-items: center; justify-content: space-between; padding: 16px 20px; border-bottom: 1px solid #30363d; }
    .mh h3 { font-size: 15px; font-weight: 700; }
    .xbtn { background: none; border: none; color: #8b949e; font-size: 20px; cursor: pointer; line-height: 1; }
    .xbtn:hover { color: #e6edf3; }
    .mb { padding: 20px; }
    .mf { padding: 12px 20px; border-top: 1px solid #30363d; display: flex; gap: 8px; justify-content: flex-end; }

    /* ── Toast notification ───────────────────────────────────── */
    #toast {
      position: fixed; bottom: 22px; right: 22px;
      padding: 10px 16px; background: #161b22; border: 1px solid #30363d;
      border-radius: 9px; font-size: 13px; z-index: 200;
      display: none; box-shadow: 0 8px 20px rgba(0,0,0,.4);
    }
    #toast.show { display: block; }
    #toast.ok   { border-color: #3fb950; color: #3fb950; }
    #toast.err  { border-color: #f85149; color: #f85149; }

    @media (max-width: 580px) {
      .form-grid { grid-template-columns: 1fr; }
      .stats     { grid-template-columns: repeat(2, 1fr); }
    }
  </style>
</head>
<body>

<!-- ── Header ──────────────────────────────────────────────────────────────── -->
<header>
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#58a6ff" stroke-width="2">
    <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
  </svg>
  <h1>MT5 Price Alert Monitor</h1>
  <div class="hstatus" id="hstatus"></div>
</header>

<div class="wrap">

  <!-- ── Tab bar ─────────────────────────────────────────────────────────── -->
  <div class="tabs">
    <button class="tab active" onclick="tab('alerts')">Alerts</button>
    <button class="tab"        onclick="tab('prices')">Live Prices</button>
    <button class="tab"        onclick="tab('logs')">Call Logs</button>
  </div>

  <!-- ── ALERTS tab ──────────────────────────────────────────────────────── -->
  <div id="tab-alerts">

    <!-- Stat counters (filled by JS) -->
    <div class="stats" id="statsGrid"></div>

    <!-- New alert form -->
    <div class="card">
      <div class="ch"><h2>New Alert</h2></div>
      <div class="cb">
        <div class="flash" id="fmsg"></div>
        <div class="form-grid">
          <div class="fg">
            <label>Symbol (e.g. EURUSD, GBPUSD)</label>
            <input id="fSym" placeholder="EURUSD" style="text-transform:uppercase" />
            <div class="phint" id="phint"></div>
          </div>
          <div class="fg">
            <label>Condition</label>
            <select id="fCond">
              <option value="above">Price goes ABOVE target</option>
              <option value="below">Price goes BELOW target</option>
            </select>
          </div>
          <div class="fg">
            <label>Target Price</label>
            <input id="fTarget" type="number" step="0.00001" placeholder="1.08500" />
          </div>
          <div class="fg">
            <label>Phone Number to Call</label>
            <input id="fPhone" placeholder="+15551234567" />
          </div>
          <div class="fg full">
            <label>Custom Message (optional — leave blank for auto)</label>
            <textarea id="fMsg" placeholder="e.g. EURUSD has broken above 1.09 — time to act!"></textarea>
          </div>
        </div>
        <div style="margin-top:12px">
          <button class="btn btn-g" onclick="createAlert()">Create Alert</button>
        </div>
      </div>
    </div>

    <!-- Alert list (filled by JS) -->
    <div class="card">
      <div class="ch">
        <h2>Your Alerts</h2>
        <span id="alertCount" style="font-size:12px;color:#8b949e"></span>
      </div>
      <div class="cb" id="alertsList">
        <div class="empty">No alerts yet. Create one above.</div>
      </div>
    </div>
  </div>

  <!-- ── PRICES tab ──────────────────────────────────────────────────────── -->
  <div id="tab-prices" style="display:none">
    <div class="card">
      <div class="ch">
        <h2>Live Forex Prices</h2>
        <div style="display:flex;gap:7px">
          <input id="symIn" placeholder="EURUSD"
                 style="width:120px;padding:6px 10px;border-radius:7px;
                        background:#0d1117;border:1px solid #30363d;
                        color:#e6edf3;font-size:12px" />
          <button class="btn btn-gray btn-sm" onclick="addSym()">Watch</button>
        </div>
      </div>
      <div class="cb">
        <div class="pg" id="priceGrid">
          <div class="empty" style="grid-column:1/-1">Loading...</div>
        </div>
        <p class="api-note">
          Powered by <a href="https://api.frankfurter.app" target="_blank">Frankfurter</a>
          — free API, no key needed.
          Supports all major fiat pairs (EUR, USD, GBP, JPY, CHF, AUD, CAD, NZD, etc.).
        </p>
      </div>
    </div>
  </div>

  <!-- ── CALL LOGS tab ───────────────────────────────────────────────────── -->
  <div id="tab-logs" style="display:none">
    <div class="card">
      <div class="ch"><h2>Call History</h2></div>
      <div class="cb" id="logsList">
        <div class="empty">No calls yet.</div>
      </div>
    </div>
  </div>

</div><!-- /.wrap -->

<!-- ── Edit alert modal ────────────────────────────────────────────────────── -->
<div class="overlay" id="editModal">
  <div class="modal">
    <div class="mh">
      <h3>Edit Alert</h3>
      <button class="xbtn" onclick="closeEdit()">&times;</button>
    </div>
    <div class="mb">
      <input type="hidden" id="eId" />
      <div class="form-grid">
        <div class="fg">
          <label>Symbol</label>
          <input id="eSym" style="text-transform:uppercase" />
        </div>
        <div class="fg">
          <label>Condition</label>
          <select id="eCond">
            <option value="above">Price goes ABOVE</option>
            <option value="below">Price goes BELOW</option>
          </select>
        </div>
        <div class="fg"><label>Target Price</label><input id="eTgt" type="number" step="0.00001" /></div>
        <div class="fg"><label>Phone</label><input id="ePh" /></div>
        <div class="fg full"><label>Custom Message</label><textarea id="eMsg"></textarea></div>
      </div>
    </div>
    <div class="mf">
      <button class="btn btn-gray" onclick="closeEdit()">Cancel</button>
      <button class="btn btn-g"    onclick="saveEdit()">Save</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
// ── State ──────────────────────────────────────────────────────────────────────
let curTab    = 'alerts';
let watchSyms = ['EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCAD', 'USDCHF'];
let prevPx    = {};       // previous prices for up/down colouring
let allAlerts = [];       // full alert list (needed for the edit modal)

// ── Tab switching ──────────────────────────────────────────────────────────────
function tab(t) {
  curTab = t;
  ['alerts', 'prices', 'logs'].forEach((x, i) => {
    document.getElementById('tab-' + x).style.display = (x === t) ? '' : 'none';
    document.querySelectorAll('.tab')[i].classList.toggle('active', x === t);
  });
  if (t === 'prices') loadPrices();
  if (t === 'logs')   loadLogs();
}

// ── Toast ──────────────────────────────────────────────────────────────────────
function toast(msg, type = 'ok') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show ' + type;
  setTimeout(() => el.className = '', 3000);
}

// ── Generic API helper ─────────────────────────────────────────────────────────
async function api(method, path, body) {
  const r = await fetch('/monitor' + path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || 'Request failed');
  return d;
}

// ── Price formatter ────────────────────────────────────────────────────────────
function fmt(p, sym) {
  if (p == null) return '—';
  // JPY and a few others trade to 3 decimal places, everything else to 5
  const big = ['JPY', 'KRW', 'HUF', 'IDR', 'VND'].some(c => sym?.includes(c));
  return big ? Number(p).toFixed(3) : Number(p).toFixed(5);
}

// ── Stats ──────────────────────────────────────────────────────────────────────
async function loadStats() {
  try {
    const s = await api('GET', '/stats');
    document.getElementById('statsGrid').innerHTML = `
      <div class="stat"><div class="lbl">Active</div>    <div class="val blue">${s.active}</div></div>
      <div class="stat"><div class="lbl">Triggered</div> <div class="val green">${s.triggered}</div></div>
      <div class="stat"><div class="lbl">Total</div>     <div class="val">${s.total}</div></div>
      <div class="stat"><div class="lbl">Calls</div>     <div class="val">${s.calls}</div></div>
      <div class="stat"><div class="lbl">Successful</div><div class="val green">${s.calls_ok}</div></div>
      <div class="stat"><div class="lbl">Failed</div>    <div class="val red">${s.calls_err}</div></div>
    `;
    const hs = document.getElementById('hstatus');
    hs.innerHTML = s.telnyx
      ? '<span class="hdot ok"></span>Telnyx ready'
      : '<span class="hdot err"></span>Telnyx not configured';
  } catch (e) {}
}

// ── Alert list ─────────────────────────────────────────────────────────────────
async function loadAlerts() {
  try {
    allAlerts = await api('GET', '/alerts');
    const el = document.getElementById('alertsList');
    document.getElementById('alertCount').textContent = allAlerts.length + ' alert(s)';

    if (!allAlerts.length) {
      el.innerHTML = '<div class="empty">No alerts yet.</div>';
      return;
    }

    el.innerHTML = allAlerts.map(a => {
      const badge = a.triggered
        ? '<span class="badge bt">Triggered</span>'
        : a.active
          ? '<span class="badge ba">Active</span>'
          : '<span class="badge bp">Paused</span>';
      const cond = a.condition === 'above' ? '▲ Above' : '▼ Below';
      const toggle = a.triggered
        ? `<button class="btn btn-gray btn-sm" onclick="resetA(${a.id})">Reset</button>`
        : `<button class="btn btn-gray btn-sm" onclick="toggleA(${a.id},${a.active ? 0 : 1})">${a.active ? 'Pause' : 'Resume'}</button>`;

      return `
        <div class="alert-row">
          <div class="sym">${a.symbol}</div>
          <div class="ai">
            <div class="cond">${cond}</div>
            <div class="tgt">${fmt(a.target, a.symbol)}</div>
          </div>
          <div class="lp" id="lp-${a.id}">—</div>
          ${badge}
          <div class="acts">
            ${toggle}
            <button class="btn btn-gray btn-sm" onclick="openEdit(${a.id})">Edit</button>
            <button class="btn btn-gray btn-sm" onclick="testCall(${a.id})" title="Place a test call now">📞</button>
            <button class="btn btn-red  btn-sm" onclick="delA(${a.id})">✕</button>
          </div>
        </div>`;
    }).join('');

    // Overlay live prices on each alert row
    const syms   = [...new Set(allAlerts.map(a => a.symbol))];
    const prices = await api('GET', '/prices?symbols=' + syms.join(','));
    allAlerts.forEach(a => {
      const el2 = document.getElementById('lp-' + a.id);
      if (el2 && prices[a.symbol] != null) el2.textContent = 'Live: ' + fmt(prices[a.symbol], a.symbol);
    });
  } catch (e) { console.warn(e); }
}

// ── Alert CRUD ─────────────────────────────────────────────────────────────────
async function createAlert() {
  const sym     = document.getElementById('fSym').value.trim().toUpperCase();
  const cond    = document.getElementById('fCond').value;
  const target  = parseFloat(document.getElementById('fTarget').value);
  const phone   = document.getElementById('fPhone').value.trim();
  const message = document.getElementById('fMsg').value.trim();
  const flash   = document.getElementById('fmsg');

  if (!sym || isNaN(target) || !phone) {
    flash.textContent = 'Symbol, target price, and phone are required.';
    flash.className = 'flash err'; flash.style.display = 'block';
    return;
  }
  try {
    await api('POST', '/alerts', { symbol: sym, condition: cond, target, phone, message });
    flash.textContent = 'Alert created!'; flash.className = 'flash ok'; flash.style.display = 'block';
    setTimeout(() => flash.style.display = 'none', 3000);
    document.getElementById('fSym').value    = '';
    document.getElementById('fTarget').value = '';
    document.getElementById('fMsg').value    = '';
    document.getElementById('phint').textContent = '';
    toast('Alert created');
    await loadAlerts(); await loadStats();
  } catch (e) {
    flash.textContent = e.message; flash.className = 'flash err'; flash.style.display = 'block';
  }
}

async function delA(id) {
  if (!confirm('Delete this alert?')) return;
  await api('DELETE', '/alerts/' + id);
  toast('Deleted');
  await loadAlerts(); await loadStats();
}

async function toggleA(id, active) {
  await api('PUT', '/alerts/' + id, { active });
  toast(active ? 'Resumed' : 'Paused');
  await loadAlerts();
}

async function resetA(id) {
  await api('POST', '/alerts/' + id + '/reset');
  toast('Reset — will trigger again when price hits target');
  await loadAlerts(); await loadStats();
}

async function testCall(id) {
  try {
    const r = await api('POST', '/alerts/' + id + '/test-call');
    toast(
      r.status === 'initiated' ? 'Test call placed! ID: ' + r.call_sid : 'Call failed: ' + r.error,
      r.status === 'initiated' ? 'ok' : 'err'
    );
    await loadLogs();
  } catch (e) { toast(e.message, 'err'); }
}

// Show current price hint when the user finishes typing a symbol
document.getElementById('fSym').addEventListener('blur', async () => {
  const s = document.getElementById('fSym').value.trim().toUpperCase();
  if (!s) return;
  try {
    const r = await api('GET', '/prices?symbols=' + s);
    document.getElementById('phint').textContent = r[s] != null
      ? 'Current: ' + fmt(r[s], s)
      : 'Symbol not found (use standard pairs like EURUSD)';
  } catch {}
});

// ── Edit modal ─────────────────────────────────────────────────────────────────
function openEdit(id) {
  const a = allAlerts.find(x => x.id === id);
  if (!a) return;
  document.getElementById('eId').value  = a.id;
  document.getElementById('eSym').value = a.symbol;
  document.getElementById('eCond').value = a.condition;
  document.getElementById('eTgt').value  = a.target;
  document.getElementById('ePh').value   = a.phone;
  document.getElementById('eMsg').value  = a.message || '';
  document.getElementById('editModal').classList.add('open');
}
function closeEdit() { document.getElementById('editModal').classList.remove('open'); }
async function saveEdit() {
  const id = document.getElementById('eId').value;
  await api('PUT', '/alerts/' + id, {
    symbol:    document.getElementById('eSym').value.trim().toUpperCase(),
    condition: document.getElementById('eCond').value,
    target:    parseFloat(document.getElementById('eTgt').value),
    phone:     document.getElementById('ePh').value.trim(),
    message:   document.getElementById('eMsg').value.trim(),
  });
  closeEdit(); toast('Saved'); await loadAlerts();
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeEdit(); });

// ── Live prices ────────────────────────────────────────────────────────────────
async function loadPrices() {
  try {
    const prices = await api('GET', '/prices?symbols=' + watchSyms.join(','));
    const grid   = document.getElementById('priceGrid');
    if (!Object.keys(prices).length) {
      grid.innerHTML = '<div class="empty" style="grid-column:1/-1">No prices — check symbols</div>';
      return;
    }
    grid.innerHTML = watchSyms.map(sym => {
      const p    = prices[sym];
      const prev = prevPx[sym];
      const dir  = (p != null && prev != null) ? (p > prev ? 'up' : p < prev ? 'dn' : '') : '';
      return `
        <div class="pc">
          <div class="ps">${sym}</div>
          <div class="pp ${dir}">${p != null ? fmt(p, sym) : '—'}</div>
          <div class="pu">${p != null ? new Date().toLocaleTimeString() : 'unavailable'}</div>
        </div>`;
    }).join('');
    watchSyms.forEach(s => { if (prices[s] != null) prevPx[s] = prices[s]; });
  } catch (e) {}
}

function addSym() {
  const inp = document.getElementById('symIn');
  const s   = inp.value.trim().toUpperCase();
  if (s && !watchSyms.includes(s)) { watchSyms.push(s); loadPrices(); }
  inp.value = '';
}
document.getElementById('symIn')?.addEventListener('keydown', e => { if (e.key === 'Enter') addSym(); });

// ── Call logs ──────────────────────────────────────────────────────────────────
async function loadLogs() {
  try {
    const logs = await api('GET', '/call-logs');
    const el   = document.getElementById('logsList');
    if (!logs.length) { el.innerHTML = '<div class="empty">No calls yet.</div>'; return; }
    el.innerHTML = logs.map(l => `
      <div class="log-row">
        <div class="li ${l.status === 'initiated' ? 'ok' : 'err'}">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07
                     19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 3.6 2.69h3
                     a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L7.91 9.91
                     a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7
                     A2 2 0 0 1 22 16.92z"/>
          </svg>
        </div>
        <div class="lm">
          <div><span class="ls">${l.symbol}</span> @ ${fmt(l.price, l.symbol)}</div>
          <div class="ld">
            ${l.phone} · ${l.status}
            ${l.error   ? ' — ' + l.error   : ''}
            ${l.call_sid ? ' · ID: ' + l.call_sid : ''}
          </div>
        </div>
        <div class="lt">${l.created_at ? new Date(l.created_at + 'Z').toLocaleString() : ''}</div>
      </div>`).join('');
  } catch (e) {}
}

// ── Auto-refresh every 10 seconds ─────────────────────────────────────────────
async function refresh() {
  await loadStats();
  if (curTab === 'alerts') await loadAlerts();
  if (curTab === 'prices') await loadPrices();
  if (curTab === 'logs')   await loadLogs();
}
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>"""


# =============================================================================
# Flask API routes  (prefixed /monitor/ to avoid conflicts)
# =============================================================================

@app.route("/")
def index():
    return render_template_string(DASHBOARD)


@app.route("/monitor/stats")
def api_stats():
    with db() as c:
        total     = c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        active    = c.execute("SELECT COUNT(*) FROM alerts WHERE active=1 AND triggered=0").fetchone()[0]
        triggered = c.execute("SELECT COUNT(*) FROM alerts WHERE triggered=1").fetchone()[0]
        calls     = c.execute("SELECT COUNT(*) FROM call_log").fetchone()[0]
        calls_ok  = c.execute("SELECT COUNT(*) FROM call_log WHERE status='initiated'").fetchone()[0]
        calls_err = c.execute("SELECT COUNT(*) FROM call_log WHERE status='error'").fetchone()[0]
    return jsonify(dict(
        total=total, active=active, triggered=triggered,
        calls=calls, calls_ok=calls_ok, calls_err=calls_err,
        telnyx=bool(TELNYX_API_KEY and TELNYX_FROM),
    ))


@app.route("/monitor/alerts", methods=["GET"])
def api_get_alerts():
    with db() as c:
        rows = c.execute("SELECT * FROM alerts ORDER BY id DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/monitor/alerts", methods=["POST"])
def api_create_alert():
    d      = request.get_json() or {}
    sym    = d.get("symbol", "").upper().strip()
    cond   = d.get("condition", "")
    target = d.get("target")
    phone  = (d.get("phone") or ALERT_TO or "").strip()
    msg    = d.get("message", "")

    if not sym or cond not in ("above", "below") or target is None or not phone:
        return jsonify({"error": "symbol, condition, target, and phone are required"}), 400

    with db() as c:
        cur = c.execute(
            "INSERT INTO alerts (symbol, condition, target, phone, message) VALUES (?,?,?,?,?)",
            (sym, cond, float(target), phone, msg)
        )
        c.commit()
        row = c.execute("SELECT * FROM alerts WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/monitor/alerts/<int:aid>", methods=["PUT"])
def api_update_alert(aid):
    d      = request.get_json() or {}
    fields = []
    vals   = []
    mapping = {
        "symbol": str, "condition **...**

_This response is too long to display in full._

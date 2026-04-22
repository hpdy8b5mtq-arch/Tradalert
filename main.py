"""
MT5 Price Alert Monitor
-----------------------
A single-file Flask app that:
  - Fetches live forex prices via Frankfurter API (free, no key needed)
  - Lets users set price alerts through a built-in HTML dashboard
  - Makes automated Telnyx phone calls when price conditions are met

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Telnyx credentials (from environment variables) ───────────────────────────
TELNYX_API_KEY = os.environ.get("TELNYX_API_KEY", "")
TELNYX_FROM    = os.environ.get("TELNYX_PHONE_NUMBER", "")
ALERT_TO       = os.environ.get("ALERT_PHONE_NUMBER", "")

# Public base URL for TeXML webhooks (Replit exposes this)
REPLIT_DOMAIN  = os.environ.get("REPLIT_DEV_DOMAIN", "")

DB_PATH = "alerts.db"
DEFAULT_SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF"]

# In-memory store for pending TeXML messages keyed by a short token
_pending_texml: dict[str, str] = {}
# Cached Telnyx connection_id for our phone number
_telnyx_connection_id: str | None = None


# ── Database ───────────────────────────────────────────────────────────────────

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS alerts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol       TEXT    NOT NULL,
                condition    TEXT    NOT NULL,
                target       REAL    NOT NULL,
                phone        TEXT    NOT NULL,
                message      TEXT    DEFAULT '',
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
                status     TEXT NOT NULL,
                error      TEXT,
                call_sid   TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
    log.info("Database ready")


# ── Price fetching ─────────────────────────────────────────────────────────────
# Frankfurter (https://api.frankfurter.app) — free, no API key, updates daily.
# Supports all major fiat currency pairs: EUR, USD, GBP, JPY, CHF, AUD, CAD, etc.

_cache: dict[str, tuple[float, float]] = {}   # symbol -> (price, timestamp)
CACHE_TTL = 8                                  # seconds before re-fetching

def fetch_price(symbol: str) -> float | None:
    symbol = symbol.upper().replace("/", "")
    now = time.time()
    if symbol in _cache:
        price, ts = _cache[symbol]
        if now - ts < CACHE_TTL:
            return price

    if len(symbol) != 6:
        return None
    base, quote = symbol[:3], symbol[3:]

    try:
        r = http.get(f"https://api.frankfurter.app/latest?from={base}&to={quote}", timeout=6)
        r.raise_for_status()
        price = float(r.json()["rates"][quote])
        _cache[symbol] = (price, now)
        return price
    except Exception as e:
        log.warning(f"Price fetch failed {symbol}: {e}")
        return None


# ── Telnyx helpers ─────────────────────────────────────────────────────────────

def get_telnyx_connection_id() -> str | None:
    """Look up the connection_id for our Telnyx phone number (cached after first call)."""
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
                log.info(f"Telnyx connection_id: {_telnyx_connection_id}")
                return _telnyx_connection_id
        log.warning(f"Phone number {TELNYX_FROM} not found in Telnyx account")
    except Exception as e:
        log.error(f"Failed to fetch Telnyx connection_id: {e}")
    return None


# ── TeXML webhook endpoint ─────────────────────────────────────────────────────
# Telnyx calls this URL when the outbound call is answered.
# We return TeXML (TwiML-compatible XML) to speak the alert message.

@app.route("/texml/<token>", methods=["GET", "POST"])
def serve_texml(token):
    message = _pending_texml.pop(token, "MT5 price alert triggered.")
    # Escape XML special characters
    safe_msg = (message
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))
    texml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Say voice="alice">{safe_msg}</Say>'
        '<Pause length="1"/>'
        f'<Say voice="alice">{safe_msg}</Say>'
        "</Response>"
    )
    return Response(texml, mimetype="text/xml")


# ── Telnyx phone call ──────────────────────────────────────────────────────────

def make_call(phone: str, symbol: str, price: float, condition: str, target: float,
              custom_msg: str = "") -> dict:
    spoken_msg = custom_msg or (
        f"MT5 price alert. {symbol} is now {price:.5f}, "
        f"which is {condition} your target of {target:.5f}."
    )

    if not (TELNYX_API_KEY and TELNYX_FROM):
        log.error("Telnyx credentials not set — skipping call")
        return {"status": "error", "error": "Telnyx not configured", "call_sid": None}

    connection_id = get_telnyx_connection_id()
    if not connection_id:
        return {
            "status": "error",
            "error": f"Could not find connection_id for {TELNYX_FROM} — check Telnyx dashboard",
            "call_sid": None,
        }

    # Store message so the /texml endpoint can serve it when the call is answered
    token = f"{phone.replace('+', '')}_{int(time.time())}"
    _pending_texml[token] = spoken_msg

    # Build the public webhook URL Telnyx will call on answer
    if REPLIT_DOMAIN:
        webhook_url = f"https://{REPLIT_DOMAIN}/texml/{token}"
    else:
        webhook_url = f"http://localhost:{os.environ.get('PORT', 8000)}/texml/{token}"

    log.info(f"TeXML webhook URL: {webhook_url}")

    try:
        client = telnyx.Telnyx(api_key=TELNYX_API_KEY)
        result = client.calls.dial(
            connection_id=connection_id,
            to=phone,
            from_=TELNYX_FROM,
            webhook_url=webhook_url,
            time_limit_secs=120,
        )
        # Extract call control ID from response
        call_control_id = None
        if hasattr(result, "data") and result.data:
            call_control_id = getattr(result.data, "call_control_id", None)
        if not call_control_id:
            call_control_id = str(result)

        log.info(f"Telnyx call placed to {phone} — control_id: {call_control_id}")
        return {"status": "initiated", "call_sid": call_control_id, "error": None}
    except Exception as e:
        # Clean up pending texml if call failed
        _pending_texml.pop(token, None)
        log.error(f"Telnyx error: {e}")
        return {"status": "error", "error": str(e), "call_sid": None}


# ── Alert monitor (background thread, checks every 10 s) ──────────────────────

def monitor_loop():
    log.info("Monitor started — checking alerts every 10 seconds")
    while True:
        try:
            check_alerts()
        except Exception as e:
            log.error(f"Monitor error: {e}")
        time.sleep(10)

def check_alerts():
    with db() as c:
        alerts = c.execute(
            "SELECT * FROM alerts WHERE active=1 AND triggered=0"
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
            result = make_call(a["phone"], a["symbol"], price, a["condition"], a["target"], a["message"])
            now = datetime.utcnow().isoformat()
            c.execute(
                "UPDATE alerts SET triggered=1, triggered_at=? WHERE id=?",
                (now, a["id"])
            )
            c.execute(
                "INSERT INTO call_log (symbol, price, phone, status, error, call_sid) "
                "VALUES (?,?,?,?,?,?)",
                (a["symbol"], price, a["phone"],
                 result["status"], result["error"], result["call_sid"])
            )
            c.commit()


# ── HTML dashboard (served at /) ───────────────────────────────────────────────

DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>MT5 Price Alert Monitor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh}

header{background:#161b22;border-bottom:1px solid #30363d;padding:14px 28px;display:flex;align-items:center;gap:10px}
header h1{font-size:17px;font-weight:700;color:#f0f6fc}
.hdot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:5px}
.hdot.ok{background:#3fb950;box-shadow:0 0 5px #3fb950}.hdot.err{background:#f85149}
header .hstatus{font-size:12px;color:#8b949e;margin-left:auto;display:flex;align-items:center;gap:4px}

.wrap{max-width:920px;margin:0 auto;padding:28px 20px}

.tabs{display:flex;gap:3px;margin-bottom:22px;background:#161b22;border:1px solid #30363d;border-radius:9px;padding:4px;width:fit-content}
.tab{padding:7px 18px;border-radius:6px;font-size:13px;font-weight:500;cursor:pointer;color:#8b949e;border:none;background:none;transition:all .15s}
.tab.active{background:#238636;color:#fff}
.tab:not(.active):hover{color:#e6edf3}

.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:12px;margin-bottom:26px}
.stat{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px}
.stat .lbl{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px}
.stat .val{font-size:24px;font-weight:700}
.val.blue{color:#58a6ff}.val.green{color:#3fb950}.val.red{color:#f85149}

.card{background:#161b22;border:1px solid #30363d;border-radius:12px;margin-bottom:20px}
.ch{padding:14px 20px;border-bottom:1px solid #30363d;display:flex;align-items:center;justify-content:space-between}
.ch h2{font-size:14px;font-weight:600}
.cb{padding:18px 20px}

.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.fg{display:flex;flex-direction:column;gap:5px}
.fg label{font-size:12px;color:#8b949e;font-weight:500}
.fg.full{grid-column:1/-1}
input,select,textarea{background:#0d1117;border:1px solid #30363d;border-radius:7px;padding:8px 11px;color:#e6edf3;font-size:13px;outline:none;transition:border .15s;font-family:inherit;width:100%}
input:focus,select:focus,textarea:focus{border-color:#58a6ff}
textarea{resize:vertical;min-height:56px}

.btn{padding:8px 16px;border-radius:7px;border:none;font-size:13px;font-weight:600;cursor:pointer;transition:all .15s}
.btn-g{background:#238636;color:#fff}.btn-g:hover{background:#2ea043}
.btn-sm{padding:4px 10px;font-size:12px}
.btn-gray{background:#21262d;color:#e6edf3;border:1px solid #30363d}.btn-gray:hover{border-color:#58a6ff;color:#58a6ff}
.btn-red{background:rgba(248,81,73,.1);color:#f85149;border:1px solid rgba(248,81,73,.15)}.btn-red:hover{background:rgba(248,81,73,.2)}

.alert-row{display:flex;align-items:center;gap:12px;padding:13px 0;border-bottom:1px solid #21262d}
.alert-row:last-child{border-bottom:none}
.sym{font-family:monospace;font-size:14px;font-weight:700;color:#58a6ff;min-width:68px}
.ai{flex:1}.ai .cond{font-size:11px;color:#8b949e}.ai .tgt{font-size:14px;font-weight:600}
.lp{font-family:monospace;font-size:11px;color:#8b949e;min-width:100px;text-align:right}
.badge{padding:3px 8px;border-radius:20px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em}
.ba{background:rgba(63,185,80,.12);color:#3fb950}
.bt{background:rgba(88,166,255,.12);color:#58a6ff}
.bp{background:rgba(139,148,158,.1);color:#8b949e}
.acts{display:flex;gap:5px}

.log-row{display:flex;align-items:center;gap:11px;padding:11px 0;border-bottom:1px solid #21262d;font-size:12px}
.log-row:last-child{border-bottom:none}
.li{width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.li.ok{background:rgba(63,185,80,.12);color:#3fb950}.li.err{background:rgba(248,81,73,.12);color:#f85149}
.lm{flex:1}.lm .ls{font-family:monospace;font-weight:700;color:#58a6ff}
.lm .ld{color:#8b949e;font-size:11px;margin-top:2px}
.lt{color:#8b949e;font-size:11px}

.pg{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:11px}
.pc{background:#0d1117;border:1px solid #30363d;border-radius:9px;padding:13px}
.pc .ps{font-family:monospace;font-weight:700;color:#58a6ff;font-size:13px}
.pc .pp{font-size:19px;font-weight:700;margin:3px 0 2px}
.pc .pp.up{color:#3fb950}.pc .pp.dn{color:#f85149}
.pc .pu{font-size:10px;color:#8b949e}

.empty{text-align:center;padding:36px;color:#8b949e;font-size:13px}
.flash{padding:9px 13px;border-radius:7px;font-size:12px;margin-bottom:12px;display:none}
.flash.ok{background:rgba(63,185,80,.1);border:1px solid rgba(63,185,80,.3);color:#3fb950}
.flash.err{background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.3);color:#f85149}

.overlay{position:fixed;inset:0;background:rgba(0,0,0,.65);display:none;align-items:center;justify-content:center;z-index:100;backdrop-filter:blur(3px)}
.overlay.open{display:flex}
.modal{background:#161b22;border:1px solid #30363d;border-radius:12px;width:460px;max-width:95vw}
.mh{display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid #30363d}
.mh h3{font-size:15px;font-weight:700}
.xbtn{background:none;border:none;color:#8b949e;font-size:20px;cursor:pointer;line-height:1}.xbtn:hover{color:#e6edf3}
.mb{padding:20px}.mf{padding:12px 20px;border-top:1px solid #30363d;display:flex;gap:8px;justify-content:flex-end}

#toast{position:fixed;bottom:22px;right:22px;padding:10px 16px;background:#161b22;border:1px solid #30363d;border-radius:9px;font-size:13px;z-index:200;display:none;box-shadow:0 8px 20px rgba(0,0,0,.4)}
#toast.show{display:block}#toast.ok{border-color:#3fb950;color:#3fb950}#toast.err{border-color:#f85149;color:#f85149}

.phint{font-size:12px;color:#58a6ff;margin-top:4px}
.api-note{font-size:11px;color:#8b949e;margin-top:14px}
.api-note a{color:#58a6ff}

@media(max-width:580px){.form-grid{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>

<header>
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#58a6ff" stroke-width="2">
    <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
  </svg>
  <h1>MT5 Price Alert Monitor</h1>
  <div class="hstatus" id="hstatus"></div>
</header>

<div class="wrap">
  <div class="tabs">
    <button class="tab active" onclick="tab('alerts')">Alerts</button>
    <button class="tab" onclick="tab('prices')">Live Prices</button>
    <button class="tab" onclick="tab('logs')">Call Logs</button>
  </div>

  <!-- ALERTS -->
  <div id="tab-alerts">
    <div class="stats" id="statsGrid"></div>

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

    <div class="card">
      <div class="ch"><h2>Your Alerts</h2><span id="alertCount" style="font-size:12px;color:#8b949e"></span></div>
      <div class="cb" id="alertsList"><div class="empty">No alerts yet. Create one above.</div></div>
    </div>
  </div>

  <!-- PRICES -->
  <div id="tab-prices" style="display:none">
    <div class="card">
      <div class="ch">
        <h2>Live Forex Prices</h2>
        <div style="display:flex;gap:7px">
          <input id="symIn" style="width:120px;padding:6px 10px;border-radius:7px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;font-size:12px" placeholder="EURUSD" />
          <button class="btn btn-gray btn-sm" onclick="addSym()">Watch</button>
        </div>
      </div>
      <div class="cb">
        <div class="pg" id="priceGrid"><div class="empty" style="grid-column:1/-1">Loading...</div></div>
        <p class="api-note">Powered by <a href="https://api.frankfurter.app" target="_blank">Frankfurter</a> — free API, no key needed. Supports all major fiat pairs (EUR, USD, GBP, JPY, CHF, AUD, CAD, NZD, etc.).</p>
      </div>
    </div>
  </div>

  <!-- LOGS -->
  <div id="tab-logs" style="display:none">
    <div class="card">
      <div class="ch"><h2>Call History</h2></div>
      <div class="cb" id="logsList"><div class="empty">No calls yet.</div></div>
    </div>
  </div>
</div>

<!-- Edit modal -->
<div class="overlay" id="editModal">
  <div class="modal">
    <div class="mh"><h3>Edit Alert</h3><button class="xbtn" onclick="closeEdit()">&times;</button></div>
    <div class="mb">
      <input type="hidden" id="eId" />
      <div class="form-grid">
        <div class="fg"><label>Symbol</label><input id="eSym" style="text-transform:uppercase" /></div>
        <div class="fg"><label>Condition</label>
          <select id="eCond"><option value="above">Price goes ABOVE</option><option value="below">Price goes BELOW</option></select>
        </div>
        <div class="fg"><label>Target Price</label><input id="eTgt" type="number" step="0.00001" /></div>
        <div class="fg"><label>Phone</label><input id="ePh" /></div>
        <div class="fg full"><label>Custom Message</label><textarea id="eMsg"></textarea></div>
      </div>
    </div>
    <div class="mf">
      <button class="btn btn-gray" onclick="closeEdit()">Cancel</button>
      <button class="btn btn-g" onclick="saveEdit()">Save</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
let curTab = 'alerts';
let watchSyms = ['EURUSD','GBPUSD','USDJPY','AUDUSD','USDCAD','USDCHF'];
let prevPx = {};
let allAlerts = [];

// Tab switching
function tab(t) {
  curTab = t;
  ['alerts','prices','logs'].forEach((x,i) => {
    document.getElementById('tab-'+x).style.display = x===t?'':'none';
    document.querySelectorAll('.tab')[i].classList.toggle('active', x===t);
  });
  if (t==='prices') loadPrices();
  if (t==='logs') loadLogs();
}

// Toast
function toast(msg, type='ok') {
  const el=document.getElementById('toast');
  el.textContent=msg; el.className='show '+type;
  setTimeout(()=>el.className='', 3000);
}

// API helper
async function api(method, path, body) {
  const r = await fetch('/monitor'+path, {method, headers:{'Content-Type':'application/json'}, body:body?JSON.stringify(body):undefined});
  const d = await r.json();
  if (!r.ok) throw new Error(d.error||'Request failed');
  return d;
}

// Format price
function fmt(p, sym) {
  if (p==null) return '\u2014';
  const big = ['JPY','KRW','HUF','IDR','VND'].some(c=>sym?.includes(c));
  return big ? Number(p).toFixed(3) : Number(p).toFixed(5);
}

// Stats
async function loadStats() {
  try {
    const s = await api('GET','/stats');
    document.getElementById('statsGrid').innerHTML = `
      <div class="stat"><div class="lbl">Active</div><div class="val blue">${s.active}</div></div>
      <div class="stat"><div class="lbl">Triggered</div><div class="val green">${s.triggered}</div></div>
      <div class="stat"><div class="lbl">Total</div><div class="val">${s.total}</div></div>
      <div class="stat"><div class="lbl">Calls</div><div class="val">${s.calls}</div></div>
      <div class="stat"><div class="lbl">Successful</div><div class="val green">${s.calls_ok}</div></div>
      <div class="stat"><div class="lbl">Failed</div><div class="val red">${s.calls_err}</div></div>
    `;
    const hs = document.getElementById('hstatus');
    if (s.telnyx) hs.innerHTML='<span class="hdot ok"></span>Telnyx ready';
    else hs.innerHTML='<span class="hdot err"></span>Telnyx not configured';
  } catch(e){}
}

// Alerts
async function loadAlerts() {
  try {
    allAlerts = await api('GET','/alerts');
    const el = document.getElementById('alertsList');
    document.getElementById('alertCount').textContent = allAlerts.length+' alert(s)';
    if (!allAlerts.length) { el.innerHTML='<div class="empty">No alerts yet.</div>'; return; }
    el.innerHTML = allAlerts.map(a => {
      const badge = a.triggered
        ? '<span class="badge bt">Triggered</span>'
        : a.active ? '<span class="badge ba">Active</span>' : '<span class="badge bp">Paused</span>';
      const cond = a.condition==='above'?'\u25b2 Above':'\u25bc Below';
      return `<div class="alert-row">
        <div class="sym">${a.symbol}</div>
        <div class="ai"><div class="cond">${cond}</div><div class="tgt">${fmt(a.target,a.symbol)}</div></div>
        <div class="lp" id="lp-${a.id}">\u2014</div>
        ${badge}
        <div class="acts">
          ${a.triggered
            ? `<button class="btn btn-gray btn-sm" onclick="resetA(${a.id})">Reset</button>`
            : `<button class="btn btn-gray btn-sm" onclick="toggleA(${a.id},${a.active?0:1})">${a.active?'Pause':'Resume'}</button>`
          }
          <button class="btn btn-gray btn-sm" onclick="openEdit(${a.id})">Edit</button>
          <button class="btn btn-gray btn-sm" onclick="testCall(${a.id})" title="Place test call">&#128222;</button>
          <button class="btn btn-red btn-sm" onclick="delA(${a.id})">&#x2715;</button>
        </div>
      </div>`;
    }).join('');
    // Fetch live prices for all displayed symbols
    const syms = [...new Set(allAlerts.map(a=>a.symbol))];
    const prices = await api('GET','/prices?symbols='+syms.join(','));
    allAlerts.forEach(a => {
      const el2 = document.getElementById('lp-'+a.id);
      if (el2 && prices[a.symbol]!=null) el2.textContent='Live: '+fmt(prices[a.symbol],a.symbol);
    });
  } catch(e){ console.warn(e); }
}

async function createAlert() {
  const sym = document.getElementById('fSym').value.trim().toUpperCase();
  const cond = document.getElementById('fCond').value;
  const target = parseFloat(document.getElementById('fTarget').value);
  const phone = document.getElementById('fPhone').value.trim();
  const message = document.getElementById('fMsg').value.trim();
  const flash = document.getElementById('fmsg');
  if (!sym||isNaN(target)||!phone) {
    flash.textContent='Symbol, target price, and phone are required.';
    flash.className='flash err'; flash.style.display='block'; return;
  }
  try {
    await api('POST','/alerts',{symbol:sym,condition:cond,target,phone,message});
    flash.textContent='Alert created!'; flash.className='flash ok'; flash.style.display='block';
    setTimeout(()=>flash.style.display='none',3000);
    document.getElementById('fSym').value='';
    document.getElementById('fTarget').value='';
    document.getElementById('fMsg').value='';
    document.getElementById('phint').textContent='';
    toast('Alert created');
    await loadAlerts(); await loadStats();
  } catch(e){ flash.textContent=e.message; flash.className='flash err'; flash.style.display='block'; }
}

async function delA(id) {
  if (!confirm('Delete this alert?')) return;
  await api('DELETE','/alerts/'+id);
  toast('Deleted'); await loadAlerts(); await loadStats();
}

async function toggleA(id, active) {
  await api('PUT','/alerts/'+id,{active});
  toast(active?'Resumed':'Paused'); await loadAlerts();
}

async function resetA(id) {
  await api('POST','/alerts/'+id+'/reset');
  toast('Reset \u2014 will trigger again when price hits target'); await loadAlerts(); await loadStats();
}

async function testCall(id) {
  try {
    const r = await api('POST','/alerts/'+id+'/test-call');
    toast(r.status==='initiated'?'Test call placed! ID: '+r.call_sid:'Call failed: '+r.error, r.status==='initiated'?'ok':'err');
    await loadLogs();
  } catch(e){ toast(e.message,'err'); }
}

// Price hint on blur
document.getElementById('fSym').addEventListener('blur', async () => {
  const s = document.getElementById('fSym').value.trim().toUpperCase();
  if (!s) return;
  try {
    const r = await api('GET','/prices?symbols='+s);
    if (r[s]!=null) document.getElementById('phint').textContent = 'Current: '+fmt(r[s],s);
    else document.getElementById('phint').textContent = 'Symbol not found (use standard pairs like EURUSD)';
  } catch {}
});

// Edit modal
function openEdit(id) {
  const a = allAlerts.find(x=>x.id===id); if (!a) return;
  document.getElementById('eId').value=a.id;
  document.getElementById('eSym').value=a.symbol;
  document.getElementById('eCond').value=a.condition;
  document.getElementById('eTgt').value=a.target;
  document.getElementById('ePh').value=a.phone;
  document.getElementById('eMsg').value=a.message||'';
  document.getElementById('editModal').classList.add('open');
}
function closeEdit(){ document.getElementById('editModal').classList.remove('open'); }
async function saveEdit() {
  const id = document.getElementById('eId').value;
  await api('PUT','/alerts/'+id,{
    symbol: document.getElementById('eSym').value.trim().toUpperCase(),
    condition: document.getElementById('eCond').value,
    target: parseFloat(document.getElementById('eTgt').value),
    phone: document.getElementById('ePh').value.trim(),
    message: document.getElementById('eMsg').value.trim(),
  });
  closeEdit(); toast('Saved'); await loadAlerts();
}
document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeEdit(); });

// Live prices
async function loadPrices() {
  try {
    const prices = await api('GET','/prices?symbols='+watchSyms.join(','));
    const grid = document.getElementById('priceGrid');
    if (!Object.keys(prices).length) { grid.innerHTML='<div class="empty" style="grid-column:1/-1">No prices \u2014 check symbols</div>'; return; }
    grid.innerHTML = watchSyms.map(sym=>{
      const p=prices[sym], prev=prevPx[sym];
      const dir = p!=null&&prev!=null ? (p>prev?'up':p<prev?'dn':'') : '';
      return `<div class="pc">
        <div class="ps">${sym}</div>
        <div class="pp ${dir}">${p!=null?fmt(p,sym):'\u2014'}</div>
        <div class="pu">${p!=null?new Date().toLocaleTimeString():'unavailable'}</div>
      </div>`;
    }).join('');
    watchSyms.forEach(s=>{ if(prices[s]!=null) prevPx[s]=prices[s]; });
  } catch(e){}
}

function addSym() {
  const inp=document.getElementById('symIn');
  const s=inp.value.trim().toUpperCase();
  if (s&&!watchSyms.includes(s)){ watchSyms.push(s); loadPrices(); }
  inp.value='';
}
document.getElementById('symIn')?.addEventListener('keydown',e=>{ if(e.key==='Enter') addSym(); });

// Call logs
async function loadLogs() {
  try {
    const logs = await api('GET','/call-logs');
    const el = document.getElementById('logsList');
    if (!logs.length) { el.innerHTML='<div class="empty">No calls yet.</div>'; return; }
    el.innerHTML = logs.map(l=>`<div class="log-row">
      <div class="li ${l.status==='initiated'?'ok':'err'}">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 3.6 2.69h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L7.91 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z"/>
        </svg>
      </div>
      <div class="lm">
        <div><span class="ls">${l.symbol}</span> @ ${fmt(l.price,l.symbol)}</div>
        <div class="ld">${l.phone} \u00b7 ${l.status}${l.error?' \u2014 '+l.error:''}${l.call_sid?' \u00b7 ID: '+l.call_sid:''}</div>
      </div>
      <div class="lt">${l.created_at?new Date(l.created_at+'Z').toLocaleString():''}</div>
    </div>`).join('');
  } catch(e){}
}

// Polling every 10s
async function refresh() {
  await loadStats();
  if (curTab==='alerts') await loadAlerts();
  if (curTab==='prices') await loadPrices();
  if (curTab==='logs') await loadLogs();
}
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>"""


# ── API routes ─────────────────────────────────────────────────────────────────

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
    d = request.get_json() or {}
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
    d = request.get_json() or {}
    fields, vals = [], []
    mapping = {"symbol": str, "condition": str, "target": float,
                "phone": str, "message": str, "active": int}
    for k, cast in mapping.items():
        if k in d:
            v = d[k]
            if k == "symbol": v = v.upper().strip()
            fields.append(f"{k}=?")
            vals.append(cast(v))
    if not fields:
        return jsonify({"error": "nothing to update"}), 400
    vals.append(aid)
    with db() as c:
        c.execute(f"UPDATE alerts SET {','.join(fields)} WHERE id=?", vals)
        c.commit()
        row = c.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
    return jsonify(dict(row))

@app.route("/monitor/alerts/<int:aid>", methods=["DELETE"])
def api_delete_alert(aid):
    with db() as c:
        c.execute("DELETE FROM alerts WHERE id=?", (aid,))
        c.commit()
    return jsonify({"ok": True})

@app.route("/monitor/alerts/<int:aid>/reset", methods=["POST"])
def api_reset_alert(aid):
    with db() as c:
        c.execute("UPDATE alerts SET triggered=0, triggered_at=NULL, active=1 WHERE id=?", (aid,))
        c.commit()
        row = c.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
    return jsonify(dict(row))

@app.route("/monitor/alerts/<int:aid>/test-call", methods=["POST"])
def api_test_call(aid):
    with db() as c:
        row = c.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    price = fetch_price(row["symbol"]) or row["target"]
    result = make_call(row["phone"], row["symbol"], price,
                       row["condition"], row["target"], row["message"])
    with db() as c:
        c.execute(
            "INSERT INTO call_log (symbol, price, phone, status, error, call_sid) VALUES (?,?,?,?,?,?)",
            (row["symbol"], price, row["phone"],
             result["status"], result["error"], result["call_sid"])
        )
        c.commit()
    return jsonify(result)

@app.route("/monitor/prices")
def api_prices():
    syms_param = request.args.get("symbols", "")
    syms = [s.strip().upper() for s in syms_param.split(",") if s.strip()] if syms_param else DEFAULT_SYMBOLS
    result = {}
    for sym in syms:
        p = fetch_price(sym)
        if p is not None:
            result[sym] = p
    return jsonify(result)

@app.route("/monitor/call-logs")
def api_call_logs():
    with db() as c:
        rows = c.execute("SELECT * FROM call_log ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify([dict(r) for r in rows])


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    monitor = threading.Thread(target=monitor_loop, daemon=True)
    monitor.start()
    port = int(os.environ.get("PORT", 8000))
    log.info(f"Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

import os
import json
import sqlite3
import hashlib
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)

# ----------------------------
# CORS
# ----------------------------
# Default: allow all (easy for dev)
# If you want to tighten later, set:
#   CORS_ORIGINS=https://your-vercel-domain.vercel.app
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")
if (CORS_ORIGINS or "").strip() == "*":
    CORS(app, resources={r"/*": {"origins": "*"}})
else:
    allowed = [o.strip() for o in (CORS_ORIGINS or "").split(",") if o.strip()]
    CORS(app, resources={r"/*": {"origins": allowed}})

# ----------------------------
# Settings (bankroll)
# ----------------------------
# Put this on Render as: /var/data/settings.json so it persists across deploys/restarts
SETTINGS_PATH = os.getenv("SETTINGS_PATH", "/var/data/settings.json")
GBPUSD_RATE = float(os.getenv("GBPUSD_RATE", "1.27"))  # simple fixed rate

def _ensure_parent_dir(path: str):
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

def load_settings():
    try:
        if not os.path.exists(SETTINGS_PATH):
            return {"bankroll_gbp": 100.0}
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"bankroll_gbp": 100.0}
        if "bankroll_gbp" not in data:
            data["bankroll_gbp"] = 100.0
        return data
    except Exception:
        return {"bankroll_gbp": 100.0}

def save_settings(data: dict):
    _ensure_parent_dir(SETTINGS_PATH)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)

def get_bankroll_gbp() -> float:
    s = load_settings()
    try:
        return float(s.get("bankroll_gbp", 100.0))
    except Exception:
        return 100.0

def set_bankroll_gbp(v: float) -> float:
    v = max(0.0, float(v))
    s = load_settings()
    s["bankroll_gbp"] = v
    save_settings(s)
    return v

# ----------------------------
# Database config
# ----------------------------
DB_PATH = os.getenv("DB_PATH", "/var/data/data.db")

def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def ensure_db_dir():
    parent = os.path.dirname(DB_PATH)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

def get_conn():
    ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _safe_json_loads(s):
    if s is None:
        return None
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s)
    except Exception:
        return None

def _to_epoch(iso_utc: str) -> int:
    try:
        s = (iso_utc or "").replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return int(datetime.now(timezone.utc).timestamp())

def _epoch_to_iso(epoch_s: int) -> str:
    try:
        return datetime.fromtimestamp(int(epoch_s), tz=timezone.utc).replace(microsecond=0).isoformat()
    except Exception:
        return utc_now_iso()

def _safe_markets_list(m):
    """
    Accept markets in any of these shapes:
      - list: ["BTCUSDT","ETHUSDT"]
      - json string: '["BTCUSDT","ETHUSDT"]'
      - single string: "BTCUSDT"
      - None/empty
    Always returns a list[str].
    """
    if m is None:
        return []
    if isinstance(m, list):
        return [str(x) for x in m if str(x).strip()]
    if isinstance(m, str):
        s = m.strip()
        if not s:
            return []
        parsed = _safe_json_loads(s)
        if isinstance(parsed, list):
            return [str(x) for x in parsed if str(x).strip()]
        return [s]
    return []

# ----------------------------
# Schema
# ----------------------------
SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS control (
      id INTEGER PRIMARY KEY CHECK (id = 1),
      state TEXT DEFAULT 'ACTIVE',              -- ACTIVE | CRYO | PAUSED
      pause_reason TEXT DEFAULT '',
      pause_until_utc TEXT DEFAULT '',
      cryo_reason TEXT DEFAULT '',
      cryo_until_utc TEXT DEFAULT '',
      updated_time_utc TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS heartbeat (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
      time_epoch INTEGER NOT NULL,
      equity_usd REAL DEFAULT 0,
      wins INTEGER DEFAULT 0,
      losses INTEGER DEFAULT 0,
      total_trades INTEGER DEFAULT 0,
      total_pnl_usd REAL DEFAULT 0,
      markets TEXT DEFAULT '[]',          -- JSON list
      open_positions INTEGER DEFAULT 0,
      prices_ok INTEGER DEFAULT 0,        -- 0/1
      status TEXT DEFAULT 'stopped',
      survival_mode TEXT DEFAULT 'NORMAL'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pet (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
      time_epoch INTEGER NOT NULL,
      fainted_until_utc TEXT DEFAULT '',
      growth REAL DEFAULT 0,
      health REAL DEFAULT 100,
      hunger REAL DEFAULT 0,
      mood TEXT DEFAULT 'neutral',
      stage TEXT DEFAULT 'egg',
      sex TEXT DEFAULT 'boy'              -- cosmetic: boy/girl
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS prices (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
      time_epoch INTEGER NOT NULL,
      market TEXT NOT NULL,
      price REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS equity (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
      time_epoch INTEGER NOT NULL,
      equity_usd REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trades (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
      time_epoch INTEGER NOT NULL,
      market TEXT NOT NULL,
      side TEXT NOT NULL,                -- buy/sell
      size_usd REAL DEFAULT 0,
      price REAL DEFAULT 0,
      pnl_usd REAL DEFAULT 0,
      confidence REAL DEFAULT 0,
      reason TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
      time_epoch INTEGER NOT NULL,
      type TEXT DEFAULT 'info',
      message TEXT DEFAULT '',
      details TEXT DEFAULT ''            -- JSON
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deaths (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
      time_epoch INTEGER NOT NULL,
      source TEXT DEFAULT 'bot',
      reason TEXT DEFAULT '',
      details TEXT DEFAULT ''            -- JSON
    )
    """
]

# ----------------------------
# Schema lock (NEW)
# ----------------------------
SCHEMA_VERSION = int(os.getenv("SCHEMA_VERSION", "1"))

def _schema_hash() -> str:
    # Normalise to reduce accidental whitespace-only changes
    joined = "\n".join([s.strip() for s in SCHEMA]).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()

def init_db():
    """
    Creates tables if missing AND locks the schema.
    If the schema changes in code but the DB already exists, it will refuse to boot.
    """
    current_hash = _schema_hash()

    conn = get_conn()
    cur = conn.cursor()

    # 1) Create schema_meta (always)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          schema_version INTEGER NOT NULL,
          schema_hash TEXT NOT NULL,
          locked_at_utc TEXT NOT NULL
        )
        """
    )

    # 2) Check existing lock
    cur.execute("SELECT schema_version, schema_hash FROM schema_meta WHERE id=1")
    row = cur.fetchone()

    if row is None:
        # First run: lock schema to whatever is in code right now
        cur.execute(
            "INSERT INTO schema_meta (id, schema_version, schema_hash, locked_at_utc) VALUES (1, ?, ?, ?)",
            (SCHEMA_VERSION, current_hash, utc_now_iso())
        )
    else:
        db_version = int(row["schema_version"])
        db_hash = str(row["schema_hash"])

        if db_version != SCHEMA_VERSION or db_hash != current_hash:
            conn.close()
            raise RuntimeError(
                "SCHEMA LOCKED: database schema does not match code.\n"
                f"DB version/hash: {db_version}/{db_hash}\n"
                f"CODE version/hash: {SCHEMA_VERSION}/{current_hash}\n"
                "If you intended to change schema, do a proper migration (don’t hot-change tables)."
            )

    # 3) Create application tables
    for stmt in SCHEMA:
        cur.execute(stmt)

    # 4) Ensure control row exists (id=1)
    cur.execute("SELECT id FROM control WHERE id=1")
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO control (id, state, pause_reason, pause_until_utc, cryo_reason, cryo_until_utc, updated_time_utc) "
            "VALUES (1, 'ACTIVE', '', '', '', '', ?)",
            (utc_now_iso(),)
        )

    conn.commit()
    conn.close()

init_db()

# ----------------------------
# Helpers: fetch
# ----------------------------
ALLOWED_TABLES = {"control","heartbeat","pet","prices","equity","trades","events","deaths"}

def fetch_one(table: str, order_by="id DESC"):
    if table not in ALLOWED_TABLES:
        raise ValueError("Invalid table")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM {table} ORDER BY {order_by} LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def fetch_many(table: str, limit=50, order_by="id DESC"):
    if table not in ALLOWED_TABLES:
        raise ValueError("Invalid table")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM {table} ORDER BY {order_by} LIMIT ?", (int(limit),))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def insert_row(table: str, data: dict):
    if table not in ALLOWED_TABLES:
        raise ValueError("Invalid table")
    conn = get_conn()
    cur = conn.cursor()
    cols = list(data.keys())
    vals = [data[c] for c in cols]
    placeholders = ",".join(["?"] * len(cols))
    cur.execute(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
        vals
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id

def add_event(ev_type: str, message: str, details=None):
    details = details or {}
    t = utc_now_iso()
    insert_row("events", {
        "time_utc": t,
        "time_epoch": _to_epoch(t),
        "type": ev_type,
        "message": message,
        "details": json.dumps(details)
    })

def get_control():
    c = fetch_one("control", order_by="id ASC")
    if not c:
        return {"id": 1, "state": "ACTIVE", "pause_reason":"", "pause_until_utc":"", "cryo_reason":"", "cryo_until_utc":"", "updated_time_utc": utc_now_iso()}
    return c

def _set_control_state(state: str, reason: str = ""):
    state = (state or "ACTIVE").upper()
    conn = get_conn()
    cur = conn.cursor()

    if state == "ACTIVE":
        cur.execute(
            "UPDATE control SET state='ACTIVE', pause_reason='', pause_until_utc='', cryo_reason='', cryo_until_utc='', updated_time_utc=? WHERE id=1",
            (utc_now_iso(),)
        )
        conn.commit()
        conn.close()
        add_event("info", "State -> ACTIVE", {"reason": reason})
        return

    conn.close()

def is_paused_or_cryo():
    c = get_control()
    now = datetime.now(timezone.utc)
    state = (c.get("state") or "ACTIVE").upper()

    pause_until = (c.get("pause_until_utc") or "").replace("Z", "+00:00")
    cryo_until = (c.get("cryo_until_utc") or "").replace("Z", "+00:00")

    paused = False
    cryo = False

    if state == "PAUSED" and pause_until:
        try:
            dt = datetime.fromisoformat(pause_until)
            paused = dt > now
        except Exception:
            paused = True

    if state == "CRYO" and cryo_until:
        try:
            dt = datetime.fromisoformat(cryo_until)
            cryo = dt > now
        except Exception:
            cryo = True

    # Auto-thaw
    if state in ("PAUSED","CRYO") and not paused and not cryo:
        _set_control_state("ACTIVE", reason="timer complete")
        c = get_control()
        state = "ACTIVE"

    return state, c

# ----------------------------
# OHLC aggregation (candles from tick prices)
# ----------------------------
def compute_ohlc(market: str, interval_sec: int = 60, limit: int = 200):
    market = (market or "").strip()
    interval_sec = max(10, int(interval_sec))
    limit = max(10, min(1000, int(limit)))

    conn = get_conn()
    rows = conn.execute(
        """
        SELECT time_epoch, price
        FROM prices
        WHERE market = ?
        ORDER BY time_epoch DESC
        LIMIT ?
        """,
        (market, 5000)
    ).fetchall()
    conn.close()

    if not rows:
        return []

    ticks = [{"t": int(r["time_epoch"]), "p": float(r["price"])} for r in rows][::-1]

    buckets = {}
    for tick in ticks:
        b = (tick["t"] // interval_sec) * interval_sec
        if b not in buckets:
            buckets[b] = {
                "t": b,
                "time_utc": _epoch_to_iso(b),
                "o": tick["p"],
                "h": tick["p"],
                "l": tick["p"],
                "c": tick["p"]
            }
        else:
            d = buckets[b]
            d["h"] = max(d["h"], tick["p"])
            d["l"] = min(d["l"], tick["p"])
            d["c"] = tick["p"]

    out = [buckets[k] for k in sorted(buckets.keys())]
    return out[-limit:]

# ----------------------------
# Settings routes
# ----------------------------
@app.get("/settings")
def get_settings_route():
    bankroll_gbp = get_bankroll_gbp()
    bankroll_usd = bankroll_gbp * GBPUSD_RATE
    return jsonify({
        "bankroll_gbp": bankroll_gbp,
        "gbpusd_rate": GBPUSD_RATE,
        "bankroll_usd": bankroll_usd,
    })

@app.post("/settings")
def set_settings_route():
    body = request.get_json(force=True, silent=True) or {}

    # support either bankroll_gbp OR bankroll_usd input
    if "bankroll_gbp" in body:
        bankroll_gbp = float(body.get("bankroll_gbp", 0))
    elif "bankroll_usd" in body:
        bankroll_usd = float(body.get("bankroll_usd", 0))
        bankroll_gbp = bankroll_usd / GBPUSD_RATE if GBPUSD_RATE else 0.0
    else:
        return jsonify({"ok": False, "error": "Provide bankroll_gbp (preferred) or bankroll_usd"}), 400

    bankroll_gbp = set_bankroll_gbp(bankroll_gbp)
    bankroll_usd = bankroll_gbp * GBPUSD_RATE

    add_event("info", "Settings updated", {"bankroll_gbp": bankroll_gbp, "bankroll_usd": bankroll_usd})

    return jsonify({
        "ok": True,
        "bankroll_gbp": bankroll_gbp,
        "gbpusd_rate": GBPUSD_RATE,
        "bankroll_usd": bankroll_usd,
    })

# ----------------------------
# Version route (NEW)
# ----------------------------
@app.get("/version")
def version():
    return jsonify({
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "schema_hash": _schema_hash(),
        "time_utc": utc_now_iso()
    })

# ----------------------------
# Routes
# ----------------------------
@app.get("/health")
def health():
    return jsonify({"ok": True, "time_utc": utc_now_iso()})

@app.get("/")
def home():
    parent = os.path.dirname(DB_PATH)
    return jsonify({
        "ok": True,
        "service": "crypto-ai-api",
        "time_utc": utc_now_iso(),
        "db_parent_exists": os.path.exists(parent),
        "db_path": DB_PATH,
        "endpoints": {
            "GET": ["/", "/health", "/version", "/data", "/heartbeat", "/pet", "/events", "/logs", "/equity", "/trades", "/prices", "/ohlc", "/deaths", "/control", "/settings"],
            "POST": [
                "/ingest/heartbeat", "/ingest/pet", "/ingest/event", "/ingest/equity", "/ingest/trade", "/ingest/prices", "/ingest/death",
                "/control/pause", "/control/cryo", "/control/revive",
                "/settings"
            ],
            "DELETE": ["/reset/all", "/reset/events", "/reset/trades", "/reset/equity", "/reset/deaths"]
        }
    })

@app.get("/control")
def control_get():
    return jsonify(get_control())

@app.get("/data")
def data():
    state, ctrl = is_paused_or_cryo()

    hb = fetch_one("heartbeat")
    pet = fetch_one("pet")

    equity_points = fetch_many("equity", limit=200, order_by="id DESC")
    equity_points.reverse()

    recent_trades = fetch_many("trades", limit=80, order_by="id DESC")

    latest_prices = fetch_many("prices", limit=800, order_by="id DESC")

    events = fetch_many("events", limit=250, order_by="id DESC")
    events.reverse()
    for e in events:
        e["details"] = _safe_json_loads(e.get("details"))

    deaths = fetch_many("deaths", limit=200, order_by="id DESC")
    deaths.reverse()
    for d in deaths:
        d["details"] = _safe_json_loads(d.get("details"))

    if hb:
        hb["markets"] = _safe_markets_list(_safe_json_loads(hb.get("markets")) or hb.get("markets"))
        hb["prices_ok"] = int(hb.get("prices_ok") or 0)

    total_trades = len(recent_trades)

    bankroll_gbp = get_bankroll_gbp()
    bankroll_usd = bankroll_gbp * GBPUSD_RATE

    return jsonify({
        "control": ctrl,
        "state": state,
        "heartbeat": hb or {},
        "pet": pet or {},
        "equity": [{"equity_usd": float(p["equity_usd"]), "time_utc": p["time_utc"]} for p in equity_points],
        "trades": [
            {
                "time_utc": t["time_utc"],
                "market": t["market"],
                "side": t["side"],
                "size_usd": float(t.get("size_usd") or 0),
                "price": float(t.get("price") or 0),
                "pnl_usd": float(t.get("pnl_usd") or 0),
                "confidence": float(t.get("confidence") or 0),
                "reason": t.get("reason") or ""
            } for t in recent_trades
        ],
        "prices": latest_prices,
        "events": events,
        "deaths": deaths,
        "settings": {
            "bankroll_gbp": bankroll_gbp,
            "gbpusd_rate": GBPUSD_RATE,
            "bankroll_usd": bankroll_usd,
        },
        "stats": {
            "paused": state in ("PAUSED","CRYO"),
            "state": state,
            "pause_until_utc": ctrl.get("pause_until_utc",""),
            "pause_reason": ctrl.get("pause_reason",""),
            "cryo_until_utc": ctrl.get("cryo_until_utc",""),
            "cryo_reason": ctrl.get("cryo_reason",""),
            "total_trades_loaded": total_trades,
        }
    })

@app.get("/ohlc")
def ohlc():
    market = request.args.get("market", "BTCUSDT")
    interval = int(request.args.get("interval", "60"))
    limit = int(request.args.get("limit", "200"))
    candles = compute_ohlc(market=market, interval_sec=interval, limit=limit)
    return jsonify({
        "market": market,
        "interval_sec": interval,
        "candles": candles
    })

@app.get("/heartbeat")
def get_heartbeat():
    return jsonify(fetch_one("heartbeat") or {})

@app.get("/pet")
def get_pet():
    return jsonify(fetch_one("pet") or {})

@app.get("/events")
def get_events():
    ev = fetch_many("events", limit=250)
    for e in ev:
        e["details"] = _safe_json_loads(e.get("details"))
    return jsonify(ev)

@app.get("/logs")
def get_logs():
    limit = int(request.args.get("limit", "120"))
    limit = max(10, min(500, limit))

    ev = fetch_many("events", limit=limit, order_by="id DESC")
    lines = []
    for e in ev:
        t = (e.get("time_utc") or "").replace("T", " ").replace("+00:00", "Z")
        typ = (e.get("type") or "info").upper()
        msg = e.get("message") or ""
        lines.append(f"{t} [{typ}] {msg}")

    return jsonify(lines)

@app.get("/equity")
def get_equity():
    points = fetch_many("equity", limit=400, order_by="id DESC")
    points.reverse()
    return jsonify(points)

@app.get("/trades")
def get_trades():
    return jsonify(fetch_many("trades", limit=300))

@app.get("/prices")
def get_prices():
    return jsonify(fetch_many("prices", limit=1000))

@app.get("/deaths")
def get_deaths():
    d = fetch_many("deaths", limit=300)
    for x in d:
        x["details"] = _safe_json_loads(x.get("details"))
    return jsonify(d)

# ----------------------------
# Ingest endpoints
# ----------------------------
@app.post("/ingest/equity")
def ingest_equity():
    body = request.get_json(force=True, silent=True) or {}
    equity_usd = float(body.get("equity_usd", 0))
    time_utc = body.get("time_utc") or utc_now_iso()
    insert_row("equity", {"time_utc": time_utc, "time_epoch": _to_epoch(time_utc), "equity_usd": equity_usd})
    return jsonify({"ok": True})

@app.post("/ingest/heartbeat")
def ingest_heartbeat():
    body = request.get_json(force=True, silent=True) or {}
    time_utc = body.get("time_utc") or utc_now_iso()
    row = {
        "time_utc": time_utc,
        "time_epoch": _to_epoch(time_utc),
        "equity_usd": float(body.get("equity_usd", 0)),
        "wins": int(body.get("wins", 0)),
        "losses": int(body.get("losses", 0)),
        "total_trades": int(body.get("total_trades", 0)),
        "total_pnl_usd": float(body.get("total_pnl_usd", 0)),
        "markets": json.dumps(body.get("markets", [])),
        "open_positions": int(body.get("open_positions", 0)),
        "prices_ok": int(bool(body.get("prices_ok", False))),
        "status": body.get("status", "running"),
        "survival_mode": body.get("survival_mode", "NORMAL"),
    }
    insert_row("heartbeat", row)
    return jsonify({"ok": True})

@app.post("/ingest/pet")
def ingest_pet():
    body = request.get_json(force=True, silent=True) or {}
    time_utc = body.get("time_utc") or utc_now_iso()
    row = {
        "time_utc": time_utc,
        "time_epoch": _to_epoch(time_utc),
        "fainted_until_utc": body.get("fainted_until_utc", "") or "",
        "growth": float(body.get("growth", 0)),
        "health": float(body.get("health", 100)),
        "hunger": float(body.get("hunger", 0)),
        "mood": body.get("mood", "neutral"),
        "stage": body.get("stage", "egg"),
        "sex": body.get("sex", "boy"),
    }
    insert_row("pet", row)
    return jsonify({"ok": True})

@app.post("/ingest/trade")
def ingest_trade():
    body = request.get_json(force=True, silent=True) or {}
    time_utc = body.get("time_utc") or utc_now_iso()
    row = {
        "time_utc": time_utc,
        "time_epoch": _to_epoch(time_utc),
        "market": body.get("market", "BTCUSDT"),
        "side": body.get("side", "buy"),
        "size_usd": float(body.get("size_usd", 0)),
        "price": float(body.get("price", 0)),
        "pnl_usd": float(body.get("pnl_usd", 0)),
        "confidence": float(body.get("confidence", 0)),
        "reason": body.get("reason", "") or "",
    }
    insert_row("trades", row)
    return jsonify({"ok": True})

@app.post("/ingest/prices")
def ingest_prices():
    body = request.get_json(force=True, silent=True) or {}
    time_utc = body.get("time_utc") or utc_now_iso()
    time_epoch = _to_epoch(time_utc)

    prices = body.get("prices", None)
    if prices is None:
        prices = body

    if not isinstance(prices, dict):
        return jsonify({"ok": False, "error": "prices must be a dict"}), 400

    count = 0
    for market, price in prices.items():
        try:
            insert_row("prices", {"time_utc": time_utc, "time_epoch": time_epoch, "market": str(market), "price": float(price)})
            count += 1
        except Exception:
            pass

    return jsonify({"ok": True, "count": count})

@app.post("/ingest/event")
def ingest_event():
    body = request.get_json(force=True, silent=True) or {}
    t = body.get("time_utc") or utc_now_iso()
    insert_row("events", {
        "time_utc": t,
        "time_epoch": _to_epoch(t),
        "type": body.get("type", "info"),
        "message": body.get("message", "") or "",
        "details": json.dumps(body.get("details", {}))
    })
    return jsonify({"ok": True})

@app.post("/ingest/death")
def ingest_death():
    body = request.get_json(force=True, silent=True) or {}
    t = body.get("time_utc") or utc_now_iso()
    insert_row("deaths", {
        "time_utc": t,
        "time_epoch": _to_epoch(t),
        "source": body.get("source", "bot"),
        "reason": body.get("reason", "") or "",
        "details": json.dumps(body.get("details", {}))
    })
    add_event("warning", "Death/Cryo record added", {"reason": body.get("reason",""), "source": body.get("source","bot")})
    return jsonify({"ok": True})

# ----------------------------
# Control endpoints
# ----------------------------
@app.post("/control/pause")
def control_pause():
    body = request.get_json(force=True, silent=True) or {}
    seconds = int(body.get("seconds", 600))
    reason = body.get("reason", "manual pause")

    until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE control SET state='PAUSED', pause_reason=?, pause_until_utc=?, updated_time_utc=? WHERE id=1",
        (reason, until, utc_now_iso())
    )
    conn.commit()
    conn.close()

    add_event("warning", "State -> PAUSED", {"pause_until_utc": until, "reason": reason})
    return jsonify({"ok": True, "state": "PAUSED", "pause_until_utc": until, "reason": reason})

@app.post("/control/cryo")
def control_cryo():
    body = request.get_json(force=True, silent=True) or {}
    seconds = int(body.get("seconds", 600))
    reason = body.get("reason", "cryo safety")

    until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE control SET state='CRYO', cryo_reason=?, cryo_until_utc=?, updated_time_utc=? WHERE id=1",
        (reason, until, utc_now_iso())
    )
    conn.commit()
    conn.close()

    add_event("warning", "State -> CRYO", {"cryo_until_utc": until, "reason": reason})
    return jsonify({"ok": True, "state": "CRYO", "cryo_until_utc": until, "reason": reason})

@app.post("/control/revive")
def control_revive():
    body = request.get_json(force=True, silent=True) or {}
    reason = body.get("reason", "revive")

    _set_control_state("ACTIVE", reason=reason)
    add_event("info", "Revive executed", {"reason": reason})
    return jsonify({"ok": True, "state": "ACTIVE"})

# ----------------------------
# Reset endpoints
# ----------------------------
def wipe_table(name):
    if name not in ALLOWED_TABLES:
        raise ValueError("Invalid table")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {name}")
    conn.commit()
    conn.close()

@app.delete("/reset/all")
def reset_all():
    for t in ["heartbeat","pet","prices","equity","trades","events","deaths"]:
        wipe_table(t)
    _set_control_state("ACTIVE", reason="reset/all")
    return jsonify({"ok": True})

@app.delete("/reset/events")
def reset_events():
    wipe_table("events")
    return jsonify({"ok": True})

@app.delete("/reset/trades")
def reset_trades():
    wipe_table("trades")
    return jsonify({"ok": True})

@app.delete("/reset/equity")
def reset_equity():
    wipe_table("equity")
    return jsonify({"ok": True})

@app.delete("/reset/deaths")
def reset_deaths():
    wipe_table("deaths")
    return jsonify({"ok": True})

# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

import os
import json
import sqlite3
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)

# ✅ CORS: allow Vercel dashboard to call Render API
# You can tighten later to your Vercel domain.
CORS(app, resources={r"/*": {"origins": "*"}})

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
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None

def _to_epoch(iso_utc: str) -> int:
    # iso_utc should be ISO; tolerate Z
    try:
        s = (iso_utc or "").replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return int(datetime.now(timezone.utc).timestamp())

# ----------------------------
# Schema (append-only where it matters)
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

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    for stmt in SCHEMA:
        cur.execute(stmt)

    # Ensure control row exists (id=1)
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

    # Auto-thaw: if timers elapsed, return ACTIVE
    if state in ("PAUSED","CRYO") and not paused and not cryo:
        # timer is done -> thaw
        _set_control_state("ACTIVE", reason="timer complete")
        c = get_control()
        state = "ACTIVE"

    return state, c

def _set_control_state(state: str, reason: str = ""):
    state = (state or "ACTIVE").upper()
    c = get_control()

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

    if state == "PAUSED":
        # keep existing pause fields; caller sets them
        conn.close()
        return

    if state == "CRYO":
        # keep existing cryo fields; caller sets them
        conn.close()
        return

    conn.close()

# ----------------------------
# OHLC aggregation (candles from tick prices)
# ----------------------------
def compute_ohlc(market: str, interval_sec: int = 60, limit: int = 200):
    """
    Builds OHLC from tick stream stored in `prices`.
    interval_sec: candle size in seconds (e.g. 60, 300, 900)
    """
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
        (market, 5000)  # pull a chunk and then bucket
    ).fetchall()
    conn.close()

    if not rows:
        return []

    # We pulled DESC; process ASC
    ticks = [{"t": int(r["time_epoch"]), "p": float(r["price"])} for r in rows][::-1]

    buckets = {}
    for tick in ticks:
        b = (tick["t"] // interval_sec) * interval_sec
        if b not in buckets:
            buckets[b] = {"t": b, "o": tick["p"], "h": tick["p"], "l": tick["p"], "c": tick["p"]}
        else:
            d = buckets[b]
            d["h"] = max(d["h"], tick["p"])
            d["l"] = min(d["l"], tick["p"])
            d["c"] = tick["p"]

    # sort by time and return last N
    out = [buckets[k] for k in sorted(buckets.keys())]
    return out[-limit:]

# ----------------------------
# Routes
# ----------------------------
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
            "GET": ["/", "/data", "/heartbeat", "/pet", "/events", "/equity", "/trades", "/prices", "/ohlc", "/deaths", "/control"],
            "POST": [
                "/ingest/heartbeat", "/ingest/pet", "/ingest/event", "/ingest/equity", "/ingest/trade", "/ingest/prices", "/ingest/death",
                "/control/pause", "/control/cryo", "/control/revive"
            ],
            "DELETE": ["/reset/all", "/reset/events", "/reset/trades", "/reset/equity", "/reset/deaths"]
        }
    })

@app.get("/control")
def control_get():
    return jsonify(get_control())

@app.get("/data")
def data():
    # Dashboard calls this
    state, ctrl = is_paused_or_cryo()

    hb = fetch_one("heartbeat")
    pet = fetch_one("pet")

    equity_points = fetch_many("equity", limit=200, order_by="id DESC")
    equity_points.reverse()

    recent_trades = fetch_many("trades", limit=80, order_by="id DESC")

    # last ticks (latest first)
    latest_prices = fetch_many("prices", limit=800, order_by="id DESC")

    # events
    events = fetch_many("events", limit=250, order_by="id DESC")
    events.reverse()
    for e in events:
        e["details"] = _safe_json_loads(e.get("details"))

    # deaths
    deaths = fetch_many("deaths", limit=200, order_by="id DESC")
    deaths.reverse()
    for d in deaths:
        d["details"] = _safe_json_loads(d.get("details"))

    # normalize
    if hb:
        hb["markets"] = _safe_json_loads(hb.get("markets")) or []
        hb["prices_ok"] = int(hb.get("prices_ok") or 0)

    # very simple stats from latest heartbeat + trades table
    total_trades = len(recent_trades)  # for UI; real totals are in heartbeat
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
        "prices": latest_prices,   # ticks (time_utc, market, price)
        "events": events,
        "deaths": deaths,
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

    # Accept either:
    # { "prices": {"BTCUSDT": 42000, "ETHUSDT": 2200} }
    # OR { "BTCUSDT": 42000, ... }
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
# Control endpoints (CRYO / PAUSE / REVIVE)
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

    # Optional: log revive as an event
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

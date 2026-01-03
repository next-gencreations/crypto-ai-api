import os
import json
import sqlite3
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)

# ✅ CORS: allow your Vercel dashboard to call Render API
CORS(app, resources={r"/*": {"origins": "*"}})

# ----------------------------
# Database config
# ----------------------------

DB_PATH = os.getenv("DB_PATH", "/var/data/data.db")

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

def ensure_db_dir():
    parent = os.path.dirname(DB_PATH)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

def get_conn():
    ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS control (
      id INTEGER PRIMARY KEY CHECK (id = 1),
      pause_reason TEXT DEFAULT '',
      pause_until_utc TEXT DEFAULT '',
      updated_time_utc TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS heartbeat (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
      equity_usd REAL DEFAULT 0,
      losses INTEGER DEFAULT 0,
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
      fainted_until_utc TEXT DEFAULT '',
      growth REAL DEFAULT 0,
      health REAL DEFAULT 100,
      hunger REAL DEFAULT 0,
      mood TEXT DEFAULT 'neutral',
      stage TEXT DEFAULT 'egg'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS prices (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
      market TEXT NOT NULL,
      price REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS equity (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
      equity_usd REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trades (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
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
      type TEXT DEFAULT 'info',
      message TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deaths (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
      reason TEXT DEFAULT ''
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
            "INSERT INTO control (id, pause_reason, pause_until_utc, updated_time_utc) VALUES (1, '', '', ?)",
            (utc_now_iso(),)
        )
    conn.commit()
    conn.close()

# Run init at startup so tables always exist
init_db()

# ----------------------------
# Helpers
# ----------------------------

ALLOWED_TABLES = {"control", "heartbeat", "pet", "prices", "equity", "trades", "events", "deaths"}

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
    cur.execute(f"SELECT * FROM {table} ORDER BY {order_by} LIMIT ?", (limit,))
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

def parse_json_list(s, default):
    try:
        if s is None or s == "":
            return default
        return json.loads(s)
    except Exception:
        return default

# ---- OHLC helpers (NEW) ----

def tf_to_seconds(tf: str) -> int:
    tf = (tf or "5m").strip().lower()
    if tf.endswith("s"):
        return int(tf[:-1])
    if tf.endswith("m"):
        return int(tf[:-1]) * 60
    if tf.endswith("h"):
        return int(tf[:-1]) * 3600
    if tf.endswith("d"):
        return int(tf[:-1]) * 86400
    return 300

def iso_to_epoch_seconds(iso_str: str) -> int:
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return int(dt.timestamp())

def epoch_seconds_to_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

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
            "GET": ["/", "/data", "/heartbeat", "/pet", "/events", "/equity", "/trades", "/prices", "/deaths", "/control", "/ohlc"],
            "POST": ["/ingest/heartbeat", "/ingest/pet", "/ingest/event", "/ingest/equity", "/ingest/trade", "/ingest/prices", "/ingest/death",
                     "/control/pause", "/control/revive"],
            "DELETE": ["/reset/all", "/reset/events", "/reset/trades", "/reset/equity", "/reset/deaths"]
        }
    })

@app.get("/data")
def data():
    ctrl = fetch_one("control", order_by="id ASC")
    hb = fetch_one("heartbeat")
    pet = fetch_one("pet")

    equity_points = fetch_many("equity", limit=100, order_by="id DESC")
    equity_points.reverse()

    recent_trades = fetch_many("trades", limit=50, order_by="id DESC")
    latest_prices = fetch_many("prices", limit=200, order_by="id DESC")

    if hb:
        hb["markets"] = parse_json_list(hb.get("markets"), [])
        hb["prices_ok"] = int(hb.get("prices_ok") or 0)

    # Basic stats computed from trades/deaths for dashboard convenience
    total_trades = len(recent_trades)
    wins = sum(1 for t in recent_trades if float(t.get("pnl_usd") or 0) > 0)
    win_rate = (wins / total_trades) if total_trades else 0.0
    total_pnl = sum(float(t.get("pnl_usd") or 0) for t in recent_trades)
    total_deaths = len(fetch_many("deaths", limit=1000))

    stats = {
        "total_trades": total_trades,
        "win_rate": win_rate,
        "total_pnl_usd": total_pnl,
        "total_deaths": total_deaths,
        "paused": bool((ctrl or {}).get("pause_until_utc")),
    }

    payload = {
        "control": ctrl or {"id": 1, "pause_reason": "", "pause_until_utc": "", "updated_time_utc": ""},
        "heartbeat": hb or {},
        "pet": pet or {},
        "stats": stats,
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
        "prices": latest_prices
    }
    return jsonify(payload)

@app.get("/ohlc")
def ohlc():
    """
    Build OHLC candles from your bot's own `prices` ticks.
    /ohlc?market=BTCUSDT&tf=5m&limit=120
    """
    market = request.args.get("market", "BTCUSDT").strip()
    tf = request.args.get("tf", "5m").strip().lower()
    limit = int(request.args.get("limit", 120))

    bucket = tf_to_seconds(tf)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT time_utc, price FROM prices WHERE market=? ORDER BY id DESC LIMIT ?",
        (market, 5000)
    )
    rows = cur.fetchall()
    conn.close()

    ticks = []
    for r in rows:
        try:
            ts = iso_to_epoch_seconds(r["time_utc"])
            price = float(r["price"])
            ticks.append((ts, price))
        except Exception:
            continue

    if not ticks:
        return jsonify({"market": market, "tf": tf, "candles": []})

    ticks.sort(key=lambda x: x[0])  # oldest -> newest

    buckets = {}
    for ts, price in ticks:
        start = (ts // bucket) * bucket
        c = buckets.get(start)
        if c is None:
            buckets[start] = {
                "t": epoch_seconds_to_iso(start),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
            }
        else:
            c["high"] = max(c["high"], price)
            c["low"] = min(c["low"], price)
            c["close"] = price

    out = list(buckets.values())
    out = out[-limit:]
    return jsonify({"market": market, "tf": tf, "candles": out})

@app.get("/heartbeat")
def get_heartbeat():
    return jsonify(fetch_one("heartbeat") or {})

@app.get("/pet")
def get_pet():
    return jsonify(fetch_one("pet") or {})

@app.get("/events")
def get_events():
    return jsonify(fetch_many("events", limit=200))

@app.get("/equity")
def get_equity():
    points = fetch_many("equity", limit=200, order_by="id DESC")
    points.reverse()
    return jsonify(points)

@app.get("/trades")
def get_trades():
    return jsonify(fetch_many("trades", limit=200))

@app.get("/prices")
def get_prices():
    return jsonify(fetch_many("prices", limit=500))

@app.get("/deaths")
def get_deaths():
    return jsonify(fetch_many("deaths", limit=200))

@app.get("/control")
def get_control():
    c = fetch_one("control", order_by="id ASC")
    return jsonify(c or {"id": 1, "pause_reason": "", "pause_until_utc": "", "updated_time_utc": ""})

# ----------------------------
# Ingest endpoints
# ----------------------------

@app.post("/ingest/equity")
def ingest_equity():
    body = request.get_json(force=True, silent=True) or {}
    equity_usd = float(body.get("equity_usd", 0))
    time_utc = body.get("time_utc") or utc_now_iso()
    insert_row("equity", {"time_utc": time_utc, "equity_usd": equity_usd})
    return jsonify({"ok": True})

@app.post("/ingest/heartbeat")
def ingest_heartbeat():
    body = request.get_json(force=True, silent=True) or {}
    row = {
        "time_utc": body.get("time_utc") or utc_now_iso(),
        "equity_usd": float(body.get("equity_usd", 0)),
        "losses": int(body.get("losses", 0)),
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
    row = {
        "time_utc": body.get("time_utc") or utc_now_iso(),
        "fainted_until_utc": body.get("fainted_until_utc", "") or "",
        "growth": float(body.get("growth", 0)),
        "health": float(body.get("health", 100)),
        "hunger": float(body.get("hunger", 0)),
        "mood": body.get("mood", "neutral"),
        "stage": body.get("stage", "egg"),
    }
    insert_row("pet", row)
    return jsonify({"ok": True})

@app.post("/ingest/trade")
def ingest_trade():
    body = request.get_json(force=True, silent=True) or {}
    row = {
        "time_utc": body.get("time_utc") or utc_now_iso(),
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
    prices = body.get("prices", {}) or {}
    for market, price in prices.items():
        insert_row("prices", {"time_utc": time_utc, "market": str(market), "price": float(price)})
    return jsonify({"ok": True, "count": len(prices)})

@app.post("/ingest/event")
def ingest_event():
    body = request.get_json(force=True, silent=True) or {}
    insert_row("events", {
        "time_utc": body.get("time_utc") or utc_now_iso(),
        "type": body.get("type", "info"),
        "message": body.get("message", "") or "",
    })
    return jsonify({"ok": True})

@app.post("/ingest/death")
def ingest_death():
    body = request.get_json(force=True, silent=True) or {}
    insert_row("deaths", {
        "time_utc": body.get("time_utc") or utc_now_iso(),
        "reason": body.get("reason", "") or "",
    })
    return jsonify({"ok": True})

# ----------------------------
# Control endpoints
# ----------------------------

@app.post("/control/pause")
def control_pause():
    body = request.get_json(force=True, silent=True) or {}
    minutes = int(body.get("minutes", 10))
    until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE control SET pause_reason=?, pause_until_utc=?, updated_time_utc=? WHERE id=1",
        ("manual_pause", until, utc_now_iso())
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "pause_until_utc": until})

@app.post("/control/revive")
def control_revive():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE control SET pause_reason='', pause_until_utc='', updated_time_utc=? WHERE id=1",
        (utc_now_iso(),)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

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
    for t in ["heartbeat", "pet", "prices", "equity", "trades", "events", "deaths"]:
        wipe_table(t)
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

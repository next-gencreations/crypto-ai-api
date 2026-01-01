import os
import json
import time
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from flask import Flask, request, jsonify, send_from_directory

APP_NAME = "crypto-ai-api"
DB_PATH = os.getenv("DB_PATH", "data.db")
PORT = int(os.getenv("PORT", "10000"))

# -------------------------
# Helpers
# -------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def http_get_json(url: str, timeout: int = 12) -> Optional[dict]:
    try:
        req = Request(url, headers={"User-Agent": f"{APP_NAME}/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except Exception:
        return None

# -------------------------
# SQLite
# -------------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS heartbeat (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time_utc TEXT,
        status TEXT,
        markets TEXT,
        open_positions INTEGER,
        equity_usd REAL,
        wins INTEGER,
        losses INTEGER,
        total_trades INTEGER,
        total_pnl_usd REAL,
        survival_mode TEXT,
        prices_ok INTEGER
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_time TEXT,
        exit_time TEXT,
        hold_minutes REAL,
        market TEXT,
        entry_price REAL,
        exit_price REAL,
        qty REAL,
        pnl_usd REAL,
        pnl_pct REAL,
        take_profit_pct REAL,
        stop_loss_pct REAL,
        risk_mode TEXT,
        trend_strength REAL,
        rsi REAL,
        volatility REAL,
        confidence REAL,
        reason TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS equity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time_utc TEXT,
        equity_usd REAL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS pet (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time_utc TEXT,
        stage TEXT,
        mood TEXT,
        health REAL,
        hunger REAL,
        growth REAL,
        fainted_until_utc TEXT,
        last_update_utc TEXT,
        survival_mode TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time_utc TEXT,
        type TEXT,
        message TEXT,
        details TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS training_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time_utc TEXT,
        event TEXT,
        details TEXT
    )
    """)

    conn.commit()
    conn.close()

def insert_row(table: str, payload: Dict[str, Any]):
    conn = db()
    cur = conn.cursor()
    keys = list(payload.keys())
    vals = [payload[k] for k in keys]
    q = f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join(['?']*len(keys))})"
    cur.execute(q, vals)
    conn.commit()
    conn.close()

def fetch_one(sql: str, args: tuple = ()) -> Optional[dict]:
    conn = db()
    cur = conn.cursor()
    cur.execute(sql, args)
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def fetch_all(sql: str, args: tuple = ()) -> List[dict]:
    conn = db()
    cur = conn.cursor()
    cur.execute(sql, args)
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def clear_table(table: str):
    conn = db()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()

# -------------------------
# Market data (simple)
# -------------------------

# If you already have your own price service, keep /prices and /history as-is
# (Your screenshots show /prices works, so this is fine.)
DEFAULT_MARKETS = ["BTC-USD", "ETH-USD", "SOL-USD", "LTC-USD", "ADA-USD", "BCH-USD"]

SYMBOL_MAP = {
    "BTC-USD": ("bitcoin", "usd"),
    "ETH-USD": ("ethereum", "usd"),
    "SOL-USD": ("solana", "usd"),
    "LTC-USD": ("litecoin", "usd"),
    "ADA-USD": ("cardano", "usd"),
    "BCH-USD": ("bitcoin-cash", "usd"),
}

def coingecko_prices(markets: List[str]) -> Dict[str, float]:
    ids = []
    for m in markets:
        if m in SYMBOL_MAP:
            ids.append(SYMBOL_MAP[m][0])
    if not ids:
        return {}

    url = f"https://api.coingecko.com/api/v3/simple/price?ids={','.join(ids)}&vs_currencies=usd"
    data = http_get_json(url)
    out: Dict[str, float] = {}
    if not isinstance(data, dict):
        return out

    # reverse map
    id_to_market = {SYMBOL_MAP[m][0]: m for m in markets if m in SYMBOL_MAP}
    for coin_id, blob in data.items():
        m = id_to_market.get(coin_id)
        if not m:
            continue
        usd = blob.get("usd") if isinstance(blob, dict) else None
        if isinstance(usd, (int, float)) and usd > 0:
            out[m] = float(usd)
    return out

def coingecko_history_close(market: str, limit: int = 180) -> List[float]:
    if market not in SYMBOL_MAP:
        return []
    coin_id, vs = SYMBOL_MAP[market]
    # 1-min candles aren’t always available; we’ll use "market_chart" days=1 and downsample
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart?vs_currency={vs}&days=1"
    data = http_get_json(url)
    if not isinstance(data, dict) or not isinstance(data.get("prices"), list):
        return []
    closes = [float(p[1]) for p in data["prices"] if isinstance(p, list) and len(p) >= 2]
    if not closes:
        return []
    # keep last N
    return closes[-limit:]

# -------------------------
# Flask app
# -------------------------

app = Flask(__name__)
init_db()

@app.get("/health")
def health():
    return jsonify({"ok": True, "time_utc": utc_now_iso()})

@app.get("/prices")
def prices():
    markets = request.args.get("markets", ",".join(DEFAULT_MARKETS)).split(",")
    markets = [m.strip().upper() for m in markets if m.strip()]
    out = coingecko_prices(markets)
    return jsonify(out)

@app.get("/history")
def history():
    market = (request.args.get("market", "BTC-USD") or "BTC-USD").strip().upper()
    limit = int(request.args.get("limit", "180"))
    limit = max(10, min(1000, limit))
    closes = coingecko_history_close(market, limit=limit)
    return jsonify({"market": market, "closes": closes})

# -------------------------
# Ingest endpoints
# -------------------------

@app.post("/ingest/heartbeat")
def ingest_heartbeat():
    payload = request.get_json(force=True, silent=True) or {}
    row = {
        "time_utc": payload.get("time_utc", utc_now_iso()),
        "status": payload.get("status", "running"),
        "markets": json.dumps(payload.get("markets", [])),
        "open_positions": int(payload.get("open_positions", 0) or 0),
        "equity_usd": float(payload.get("equity_usd", 0.0) or 0.0),
        "wins": int(payload.get("wins", 0) or 0),
        "losses": int(payload.get("losses", 0) or 0),
        "total_trades": int(payload.get("total_trades", 0) or 0),
        "total_pnl_usd": float(payload.get("total_pnl_usd", 0.0) or 0.0),
        "survival_mode": payload.get("survival_mode", "NORMAL"),
        "prices_ok": 1 if payload.get("prices_ok", False) else 0,
    }
    insert_row("heartbeat", row)
    return jsonify({"ok": True})

@app.post("/ingest/trade")
def ingest_trade():
    payload = request.get_json(force=True, silent=True) or {}
    # store only known keys (avoid DB errors)
    row = {
        "entry_time": payload.get("entry_time", ""),
        "exit_time": payload.get("exit_time", ""),
        "hold_minutes": float(payload.get("hold_minutes", 0.0) or 0.0),
        "market": payload.get("market", ""),
        "entry_price": float(payload.get("entry_price", 0.0) or 0.0),
        "exit_price": float(payload.get("exit_price", 0.0) or 0.0),
        "qty": float(payload.get("qty", 0.0) or 0.0),
        "pnl_usd": float(payload.get("pnl_usd", 0.0) or 0.0),
        "pnl_pct": float(payload.get("pnl_pct", 0.0) or 0.0),
        "take_profit_pct": float(payload.get("take_profit_pct", 0.0) or 0.0),
        "stop_loss_pct": float(payload.get("stop_loss_pct", 0.0) or 0.0),
        "risk_mode": payload.get("risk_mode", ""),
        "trend_strength": float(payload.get("trend_strength", 0.0) or 0.0),
        "rsi": float(payload.get("rsi", 0.0) or 0.0),
        "volatility": float(payload.get("volatility", 0.0) or 0.0),
        "confidence": float(payload.get("confidence", 0.0) or 0.0),
        "reason": payload.get("reason", "")[:500],
    }
    insert_row("trades", row)
    return jsonify({"ok": True})

@app.post("/ingest/equity")
def ingest_equity():
    payload = request.get_json(force=True, silent=True) or {}
    row = {
        "time_utc": payload.get("time_utc", utc_now_iso()),
        "equity_usd": float(payload.get("equity_usd", 0.0) or 0.0),
    }
    insert_row("equity", row)
    return jsonify({"ok": True})

@app.post("/ingest/pet")
def ingest_pet():
    payload = request.get_json(force=True, silent=True) or {}
    row = {
        "time_utc": payload.get("time_utc", utc_now_iso()),
        "stage": payload.get("stage", "egg"),
        "mood": payload.get("mood", "sleepy"),
        "health": float(payload.get("health", 100.0) or 100.0),
        "hunger": float(payload.get("hunger", 60.0) or 60.0),
        "growth": float(payload.get("growth", 0.0) or 0.0),
        "fainted_until_utc": payload.get("fainted_until_utc", ""),
        "last_update_utc": payload.get("last_update_utc", ""),
        "survival_mode": payload.get("survival_mode", "NORMAL"),
    }
    insert_row("pet", row)
    return jsonify({"ok": True})

@app.post("/ingest/event")
def ingest_event():
    payload = request.get_json(force=True, silent=True) or {}
    row = {
        "time_utc": payload.get("time_utc", utc_now_iso()),
        "type": payload.get("type", "event"),
        "message": payload.get("message", ""),
        "details": json.dumps(payload.get("details", {}))[:2000],
    }
    insert_row("events", row)
    return jsonify({"ok": True})

@app.post("/ingest/training_event")
def ingest_training_event():
    payload = request.get_json(force=True, silent=True) or {}
    row = {
        "time_utc": payload.get("time_utc", utc_now_iso()),
        "event": payload.get("event", ""),
        "details": payload.get("details", "")[:500],
    }
    insert_row("training_events", row)
    return jsonify({"ok": True})

@app.post("/reset_pet")
def reset_pet():
    # wipe pet only
    clear_table("pet")
    return jsonify({"ok": True})

# -------------------------
# Data endpoint used by dashboard
# -------------------------

@app.get("/data")
def data():
    hb = fetch_one("SELECT * FROM heartbeat ORDER BY id DESC LIMIT 1")
    pet = fetch_one("SELECT * FROM pet ORDER BY id DESC LIMIT 1")

    stats = fetch_one("""
      SELECT
        COUNT(*) as total_trades,
        SUM(CASE WHEN pnl_usd >= 0 THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) as losses,
        COALESCE(SUM(pnl_usd), 0) as total_pnl_usd,
        COALESCE(AVG(pnl_usd), 0) as avg_pnl
      FROM trades
    """) or {"total_trades": 0, "wins": 0, "losses": 0, "total_pnl_usd": 0.0, "avg_pnl": 0.0}

    # last 50 trades
    trades = fetch_all("SELECT * FROM trades ORDER BY id DESC LIMIT 50")

    # last 200 equity points
    eq = fetch_all("SELECT time_utc, equity_usd FROM equity ORDER BY id DESC LIMIT 200")
    eq.reverse()

    # last 40 events
    events = fetch_all("SELECT time_utc, type, message, details FROM events ORDER BY id DESC LIMIT 40")

    # clean up stored json strings
    if hb and isinstance(hb.get("markets"), str):
        try:
            hb["markets"] = json.loads(hb["markets"])
        except Exception:
            pass
    for ev in events:
        if isinstance(ev.get("details"), str):
            try:
                ev["details"] = json.loads(ev["details"])
            except Exception:
                pass

    return jsonify({
        "heartbeat": hb,
        "pet": pet,
        "stats": {
            "total_trades": int(stats.get("total_trades") or 0),
            "wins": int(stats.get("wins") or 0),
            "losses": int(stats.get("losses") or 0),
            "total_pnl_usd": float(stats.get("total_pnl_usd") or 0.0),
            "avg_pnl": float(stats.get("avg_pnl") or 0.0),
            "win_rate": (float(stats.get("wins") or 0) / max(float(stats.get("total_trades") or 0), 1.0)) * 100.0,
        },
        "trades": trades,
        "equity": eq,
        "events": events,
        "training_events": fetch_all("SELECT * FROM training_events ORDER BY id DESC LIMIT 20"),
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)

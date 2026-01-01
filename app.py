import os
import json
import time
import sqlite3
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from flask import Flask, request, jsonify

# ============================================================
# Logging
# ============================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("crypto-ai-api")

# ============================================================
# Config
# ============================================================

DB_PATH = os.getenv("DB_PATH", "data.db").strip() or "data.db"
PORT = int(os.getenv("PORT", "10000"))

# Markets to serve for /prices and /history (optional)
DEFAULT_MARKETS = os.getenv("MARKETS", "BTC-USD,ETH-USD,SOL-USD,ADA-USD,LTC-USD,BCH-USD")
MARKETS = [m.strip().upper() for m in DEFAULT_MARKETS.split(",") if m.strip()]

# Candle settings
COINBASE_CANDLE_GRANULARITY = int(os.getenv("CANDLE_GRANULARITY", "3600"))  # 1h

app = Flask(__name__)

# ============================================================
# Helpers
# ============================================================

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def http_get_json(url: str, timeout: int = 12) -> Optional[dict]:
    try:
        req = Request(url, headers={"User-Agent": "crypto-ai-api/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        log.warning(f"GET failed {url}: {e}")
        return None
    except Exception as e:
        log.warning(f"GET failed {url}: {e}")
        return None

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
        payload TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pet (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time_utc TEXT,
        payload TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS equity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time_utc TEXT,
        equity_usd REAL,
        payload TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time_utc TEXT,
        market TEXT,
        pnl_usd REAL,
        payload TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time_utc TEXT,
        type TEXT,
        message TEXT,
        payload TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS training_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time_utc TEXT,
        event TEXT,
        payload TEXT
    )
    """)

    conn.commit()
    conn.close()

def insert_row(table: str, time_utc: str, fields: Dict[str, Any], payload: Dict[str, Any]):
    conn = db()
    cur = conn.cursor()

    cols = ["time_utc"] + list(fields.keys()) + ["payload"]
    vals = [time_utc] + list(fields.values()) + [json.dumps(payload)]

    placeholders = ",".join(["?"] * len(cols))
    col_sql = ",".join(cols)
    cur.execute(f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})", vals)

    conn.commit()
    conn.close()

def fetch_one(table: str) -> Optional[dict]:
    conn = db()
    cur = conn.cursor()
    cur.execute(f"SELECT payload FROM {table} ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except Exception:
        return None

def fetch_many(table: str, limit: int = 200) -> List[dict]:
    conn = db()
    cur = conn.cursor()
    cur.execute(f"SELECT payload FROM {table} ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["payload"]))
        except Exception:
            pass
    out.reverse()
    return out

def compute_stats(trades: List[dict]) -> Dict[str, Any]:
    if not trades:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl_usd": 0.0,
            "avg_pnl": 0.0,
        }
    pnls = []
    wins = 0
    losses = 0
    for t in trades:
        p = float(t.get("pnl_usd", 0.0) or 0.0)
        pnls.append(p)
        if p >= 0:
            wins += 1
        else:
            losses += 1
    total = sum(pnls)
    total_trades = len(trades)
    return {
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / total_trades) * 100.0 if total_trades else 0.0,
        "total_pnl_usd": total,
        "avg_pnl": total / total_trades if total_trades else 0.0,
    }

# ============================================================
# Price endpoints (Coinbase spot + candles)
# ============================================================

def coinbase_spot_price(product: str) -> Optional[float]:
    # Coinbase public spot API
    # https://api.coinbase.com/v2/prices/BTC-USD/spot
    data = http_get_json(f"https://api.coinbase.com/v2/prices/{product}/spot")
    try:
        amt = data["data"]["amount"]
        return float(amt)
    except Exception:
        return None

def coinbase_candles(product: str, limit: int = 180) -> List[float]:
    # Coinbase Exchange candles (public)
    # https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=3600
    url = f"https://api.exchange.coinbase.com/products/{product}/candles?granularity={COINBASE_CANDLE_GRANULARITY}"
    data = http_get_json(url)
    closes: List[float] = []
    if isinstance(data, list):
        # Each item: [time, low, high, open, close, volume]
        for row in data:
            if isinstance(row, list) and len(row) >= 5:
                c = row[4]
                if isinstance(c, (int, float)) and c > 0:
                    closes.append(float(c))
    closes.reverse()  # oldest -> newest
    if limit and len(closes) > limit:
        closes = closes[-limit:]
    return closes

# ============================================================
# Routes
# ============================================================

@app.get("/health")
def health():
    return jsonify({"ok": True, "time_utc": utc_now_iso()})

@app.get("/prices")
def prices():
    out: Dict[str, float] = {}
    # If MARKETS empty, still return something safe
    markets = MARKETS or ["BTC-USD", "ETH-USD"]
    for m in markets:
        px = coinbase_spot_price(m)
        if px is not None:
            out[m] = px
    return jsonify(out)

@app.get("/history")
def history():
    market = (request.args.get("market") or "").strip().upper()
    limit = int(request.args.get("limit") or "180")
    if not market:
        return jsonify({"error": "missing market"}), 400
    closes = coinbase_candles(market, limit=limit)
    return jsonify({"market": market, "closes": closes})

@app.get("/data")
def data():
    hb = fetch_one("heartbeat")
    pet = fetch_one("pet")
    equity = fetch_many("equity", limit=400)
    trades = fetch_many("trades", limit=500)
    events = fetch_many("events", limit=300)
    training_events = fetch_many("training_events", limit=200)

    stats = compute_stats(trades)

    return jsonify({
        "heartbeat": hb,
        "pet": pet,
        "equity": equity,
        "trades": trades,
        "events": events,
        "training_events": training_events,
        "stats": stats,
    })

# ---------------- Ingest endpoints ----------------

@app.post("/ingest/heartbeat")
def ingest_heartbeat():
    payload = request.get_json(force=True, silent=True) or {}
    t = payload.get("time_utc") or utc_now_iso()
    insert_row("heartbeat", t, {}, payload)
    return jsonify({"ok": True})

@app.post("/ingest/pet")
def ingest_pet():
    payload = request.get_json(force=True, silent=True) or {}
    t = payload.get("time_utc") or utc_now_iso()
    insert_row("pet", t, {}, payload)
    return jsonify({"ok": True})

@app.post("/ingest/equity")
def ingest_equity():
    payload = request.get_json(force=True, silent=True) or {}
    t = payload.get("time_utc") or utc_now_iso()
    equity_usd = float(payload.get("equity_usd") or 0.0)
    insert_row("equity", t, {"equity_usd": equity_usd}, payload)
    return jsonify({"ok": True})

@app.post("/ingest/trade")
def ingest_trade():
    payload = request.get_json(force=True, silent=True) or {}
    t = payload.get("exit_time") or payload.get("time_utc") or utc_now_iso()
    market = str(payload.get("market") or "")
    pnl_usd = float(payload.get("pnl_usd") or 0.0)
    insert_row("trades", t, {"market": market, "pnl_usd": pnl_usd}, payload)
    return jsonify({"ok": True})

@app.post("/ingest/event")
def ingest_event():
    payload = request.get_json(force=True, silent=True) or {}
    t = payload.get("time_utc") or utc_now_iso()
    etype = str(payload.get("type") or "")
    msg = str(payload.get("message") or "")
    insert_row("events", t, {"type": etype, "message": msg}, payload)
    return jsonify({"ok": True})

@app.post("/ingest/training_event")
def ingest_training_event():
    payload = request.get_json(force=True, silent=True) or {}
    t = payload.get("time_utc") or utc_now_iso()
    ev = str(payload.get("event") or "")
    insert_row("training_events", t, {"event": ev}, payload)
    return jsonify({"ok": True})

# ============================================================
# Boot
# ============================================================

init_db()

if __name__ == "__main__":
    log.info(f"Starting api on 0.0.0.0:{PORT} db={DB_PATH}")
    app.run(host="0.0.0.0", port=PORT)

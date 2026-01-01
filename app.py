import os
import csv
import json
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

from flask import Flask, jsonify, request, send_from_directory

# -----------------------------
# App setup
# -----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATIC_DIR = os.path.join(BASE_DIR, "static")

os.makedirs(DATA_DIR, exist_ok=True)

TRADES_FILE = os.path.join(DATA_DIR, "trades.csv")
EQUITY_FILE = os.path.join(DATA_DIR, "equity_curve.csv")
TRAINING_FILE = os.path.join(DATA_DIR, "training_events.csv")
HEARTBEAT_FILE = os.path.join(DATA_DIR, "heartbeat.json")
PET_FILE = os.path.join(DATA_DIR, "pet.json")
EVENTS_FILE = os.path.join(DATA_DIR, "events.csv")  # sounds/thoughts/status

app = Flask(__name__, static_folder=STATIC_DIR)

# -----------------------------
# Helpers
# -----------------------------
def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

def ensure_csv(path, headers):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)

def append_csv(path, row):
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(row)

def read_csv(path, limit=None):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if limit is not None:
        return rows[-limit:]
    return rows

def safe_read_json(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def safe_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

def to_float(val, default=0.0):
    try:
        return float(val)
    except Exception:
        return default

# -----------------------------
# Ensure base files exist
# -----------------------------
ensure_csv(TRADES_FILE, [
    "entry_time", "exit_time", "hold_minutes", "market",
    "entry_price", "exit_price", "qty",
    "pnl_usd", "pnl_pct",
    "take_profit_pct", "stop_loss_pct",
    "risk_mode", "trend_strength", "rsi", "volatility",
    "confidence", "reason"
])
ensure_csv(EQUITY_FILE, ["time_utc", "equity_usd"])
ensure_csv(TRAINING_FILE, ["time_utc", "event", "details"])
ensure_csv(EVENTS_FILE, ["time_utc", "type", "message", "details_json"])

# -----------------------------
# Basic routes
# -----------------------------
@app.get("/")
def index():
    idx = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(idx):
        return send_from_directory(STATIC_DIR, "index.html")
    return "<h1>API is running</h1><p>Try /health, /prices, /history, /data</p>", 200

@app.get("/health")
def health():
    return "OK", 200

@app.get("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)

# -----------------------------
# Ingest endpoints (BOT -> API)
# -----------------------------
@app.post("/ingest/heartbeat")
def ingest_heartbeat():
    payload = request.json or {}
    payload["time_utc"] = payload.get("time_utc") or utc_now_iso()
    safe_write_json(HEARTBEAT_FILE, payload)
    return jsonify({"status": "ok"})

@app.post("/ingest/trade")
def ingest_trade():
    p = request.json or {}
    row = [
        p.get("entry_time", utc_now_iso()),
        p.get("exit_time", utc_now_iso()),
        p.get("hold_minutes", ""),
        p.get("market", ""),
        p.get("entry_price", ""),
        p.get("exit_price", ""),
        p.get("qty", ""),
        p.get("pnl_usd", ""),
        p.get("pnl_pct", ""),
        p.get("take_profit_pct", ""),
        p.get("stop_loss_pct", ""),
        p.get("risk_mode", ""),
        p.get("trend_strength", ""),
        p.get("rsi", ""),
        p.get("volatility", ""),
        p.get("confidence", ""),
        p.get("reason", ""),
    ]
    append_csv(TRADES_FILE, row)
    return jsonify({"status": "ok"})

@app.post("/ingest/equity")
def ingest_equity():
    p = request.json or {}
    t = p.get("time_utc") or utc_now_iso()
    eq = p.get("equity_usd", "")
    append_csv(EQUITY_FILE, [t, eq])
    return jsonify({"status": "ok"})

@app.post("/ingest/training_event")
def ingest_training_event():
    p = request.json or {}
    t = p.get("time_utc") or utc_now_iso()
    event = p.get("event", "event")
    details = p.get("details", "")
    append_csv(TRAINING_FILE, [t, event, details])
    return jsonify({"status": "ok"})

@app.post("/ingest/pet")
def ingest_pet():
    p = request.json or {}
    p["time_utc"] = p.get("time_utc") or utc_now_iso()
    safe_write_json(PET_FILE, p)
    return jsonify({"status": "ok"})

@app.post("/ingest/event")
def ingest_event():
    p = request.json or {}
    t = p.get("time_utc") or utc_now_iso()
    etype = p.get("type", "event")
    msg = p.get("message", "")
    details = p.get("details", {}) or {}
    append_csv(EVENTS_FILE, [t, etype, msg, json.dumps(details, ensure_ascii=False)])
    return jsonify({"status": "ok"})

@app.post("/pet/reset")
def pet_reset():
    safe_write_json(PET_FILE, {})
    append_csv(EVENTS_FILE, [utc_now_iso(), "status", "pet_reset", json.dumps({}, ensure_ascii=False)])
    return jsonify({"status": "ok"})

# -----------------------------
# Dashboard read endpoint
# -----------------------------
@app.get("/data")
def data():
    hb = safe_read_json(HEARTBEAT_FILE, {})
    pet = safe_read_json(PET_FILE, {})

    trades = read_csv(TRADES_FILE, limit=250)
    equity = read_csv(EQUITY_FILE, limit=800)
    training = read_csv(TRAINING_FILE, limit=200)
    events = read_csv(EVENTS_FILE, limit=120)

    for e in events:
        try:
            e["details"] = json.loads(e.get("details_json") or "{}")
        except Exception:
            e["details"] = {}

    wins = 0
    losses = 0
    total_pnl = 0.0
    for t in trades:
        pnl = to_float(t.get("pnl_usd"), 0.0)
        total_pnl += pnl
        if pnl >= 0:
            wins += 1
        else:
            losses += 1

    total = wins + losses
    win_rate = (wins / total * 100.0) if total else 0.0
    avg_pnl = (total_pnl / total) if total else 0.0

    return jsonify({
        "heartbeat": hb,
        "pet": pet,
        "events": list(reversed(events)),
        "recent_trades": list(reversed(trades)),
        "equity_series": equity,
        "training_events": list(reversed(training)),
        "stats": {
            "wins": wins,
            "losses": losses,
            "total_trades": total,
            "win_rate": round(win_rate, 2),
            "avg_pnl": round(avg_pnl, 4),
            "total_pnl_usd": round(total_pnl, 6),
        }
    })

# -----------------------------
# Market data (CoinGecko)
# -----------------------------
COINGECKO_BASE = os.environ.get("COINGECKO_BASE", "https://api.coingecko.com/api/v3").rstrip("/")
PRICE_CACHE_SECONDS = int(os.environ.get("PRICE_CACHE_SECONDS", "20"))
HISTORY_CACHE_SECONDS = int(os.environ.get("HISTORY_CACHE_SECONDS", "120"))

COIN_ID = {
    "BTC-USD": "bitcoin",
    "ETH-USD": "ethereum",
    "LTC-USD": "litecoin",
    "SOL-USD": "solana",
    "ADA-USD": "cardano",
    "BCH-USD": "bitcoin-cash",
}

_price_cache = {"t": 0.0, "data": {}}
_hist_cache = {}  # (market, limit) -> {"t":..., "closes":[...]}

def http_get_json(url: str, timeout: int = 15):
    try:
        req = Request(url, headers={"User-Agent": "crypto-ai-api/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except Exception:
        return None

def get_prices(markets):
    now = time.time()
    if (now - _price_cache["t"]) < PRICE_CACHE_SECONDS and _price_cache["data"]:
        return {m: _price_cache["data"].get(m) for m in markets if m in _price_cache["data"]}

    ids = []
    market_for_id = {}
    for m in markets:
        cid = COIN_ID.get(m)
        if cid:
            ids.append(cid)
            market_for_id[cid] = m

    if not ids:
        return {}

    url = f"{COINGECKO_BASE}/simple/price?ids={','.join(ids)}&vs_currencies=usd"
    data = http_get_json(url)

    out = {}
    if isinstance(data, dict):
        for cid, payload in data.items():
            if isinstance(payload, dict):
                usd = payload.get("usd")
                if isinstance(usd, (int, float)) and usd > 0:
                    m = market_for_id.get(cid)
                    if m:
                        out[m] = float(usd)

    _price_cache["t"] = now
    _price_cache["data"] = dict(out)
    return out

def get_history(market: str, limit: int):
    limit = max(10, min(500, int(limit)))
    key = (market, limit)
    now = time.time()

    cached = _hist_cache.get(key)
    if cached and (now - cached["t"]) < HISTORY_CACHE_SECONDS:
        return cached["closes"]

    cid = COIN_ID.get(market)
    if not cid:
        return []

    days = max(1, min(90, int((limit / 24) + 2)))
    url = f"{COINGECKO_BASE}/coins/{cid}/market_chart?vs_currency=usd&days={days}"
    data = http_get_json(url)

    closes = []
    if isinstance(data, dict) and isinstance(data.get("prices"), list):
        for row in data["prices"]:
            if isinstance(row, list) and len(row) >= 2:
                px = row[1]
                if isinstance(px, (int, float)) and px > 0:
                    closes.append(float(px))

    closes = closes[-limit:]
    _hist_cache[key] = {"t": now, "closes": closes}
    return closes

@app.get("/prices")
def prices():
    ms = request.args.get("markets", "").strip()
    markets = [m.strip().upper() for m in ms.split(",") if m.strip()] if ms else list(COIN_ID.keys())
    return jsonify(get_prices(markets))

@app.get("/history")
def history():
    market = (request.args.get("market") or "BTC-USD").strip().upper()
    limit = int(request.args.get("limit", "180"))
    closes = get_history(market, limit)
    return jsonify({"market": market, "closes": closes})

# -----------------------------
# Run
# -----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

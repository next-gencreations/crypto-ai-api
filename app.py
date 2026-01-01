import os
import csv
import json
from datetime import datetime, timezone
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

def to_float(val, default=0.0):
    try:
        return float(val)
    except Exception:
        return default

def to_int(val, default=0):
    try:
        return int(val)
    except Exception:
        return default

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

# -----------------------------
# Ensure base CSVs exist
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
# Static UI (dashboard)
# -----------------------------
@app.get("/")
def index():
    # Serve static/index.html if you have one
    if os.path.exists(os.path.join(STATIC_DIR, "index.html")):
        return send_from_directory(STATIC_DIR, "index.html")
    return "<h1>API is running</h1><p>Add static/index.html for a dashboard.</p>"

@app.get("/health")
def health():
    return "OK", 200

# -----------------------------
# Ingest endpoints (BOT → API)
# -----------------------------
@app.post("/ingest/heartbeat")
def ingest_heartbeat():
    payload = request.json or {}
    payload["time_utc"] = utc_now_iso()
    safe_write_json(HEARTBEAT_FILE, payload)
    return jsonify({"status": "ok"})

@app.post("/ingest/trade")
def ingest_trade():
    p = request.json or {}

    # Required-ish fields with safe defaults
    entry_time = p.get("entry_time") or utc_now_iso()
    exit_time = p.get("exit_time") or utc_now_iso()

    row = [
        entry_time,
        exit_time,
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
    eq = p.get("equity_usd")
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
    """
    Bot can POST current pet state here.
    Stored as JSON so dashboard can show it.
    """
    p = request.json or {}
    if "time_utc" not in p:
        p["time_utc"] = utc_now_iso()
    safe_write_json(PET_FILE, p)
    return jsonify({"status": "ok"})

@app.post("/ingest/event")
def ingest_event():
    """
    For pet noises / thoughts / status messages.
    Example:
      { "type": "sound", "message": "happy", "details": {"kind":"happy"} }
    """
    p = request.json or {}
    t = p.get("time_utc") or utc_now_iso()
    etype = p.get("type", "event")
    msg = p.get("message", "")
    details = p.get("details", {}) or {}
    append_csv(EVENTS_FILE, [t, etype, msg, json.dumps(details, ensure_ascii=False)])
    return jsonify({"status": "ok"})

# -----------------------------
# Dashboard read endpoints (UI → API)
# -----------------------------
@app.get("/data")
def data():
    """
    Single endpoint the dashboard can call.
    """
    hb = safe_read_json(HEARTBEAT_FILE, {})
    pet = safe_read_json(PET_FILE, {})

    trades = read_csv(TRADES_FILE, limit=250)
    equity = read_csv(EQUITY_FILE, limit=800)
    training = read_csv(TRAINING_FILE, limit=200)
    events = read_csv(EVENTS_FILE, limit=60)

    # Parse details_json safely
    for e in events:
        try:
            e["details"] = json.loads(e.get("details_json", "") or "{}")
        except Exception:
            e["details"] = {}

    # Metrics
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
        "events": events[::-1],          # newest first
        "recent_trades": trades[::-1],   # newest first
        "equity_series": equity,
        "training_events": training[::-1],
        "stats": {
            "wins": wins,
            "losses": losses,
            "total_trades": total,
            "win_rate": round(win_rate, 2),
            "avg_pnl": round(avg_pnl, 4),
            "total_pnl_usd": round(total_pnl, 6),
        }
    })

@app.post("/pet/reset")
def pet_reset():
    safe_write_json(PET_FILE, {})
    append_csv(EVENTS_FILE, [utc_now_iso(), "status", "pet_reset", json.dumps({}, ensure_ascii=False)])
    return jsonify({"status": "ok"})

# -----------------------------
# Optional: serve other static files
# -----------------------------
@app.get("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)

# -----------------------------
# Run
# -----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

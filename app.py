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

app = Flask(__name__, static_folder=STATIC_DIR)

# -----------------------------
# Helpers
# -----------------------------
def utc_now():
    return datetime.now(timezone.utc).isoformat()

def ensure_csv(path, headers):
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)

def append_csv(path, row):
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(row)

def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))

def to_float(val, default=0.0):
    try:
        return float(val)
    except:
        return default

# -----------------------------
# Ensure base CSVs exist
# -----------------------------
ensure_csv(TRADES_FILE, [
    "entry_time", "exit_time", "hold_minutes", "market",
    "entry_price", "exit_price", "qty",
    "pnl_usd", "pnl_pct",
    "take_profit_pct", "stop_loss_pct",
    "risk_mode", "trend_strength", "rsi", "volatility"
])

ensure_csv(EQUITY_FILE, ["time_utc", "equity_usd"])
ensure_csv(TRAINING_FILE, ["time_utc", "event", "details"])

# -----------------------------
# Ingest endpoints (BOT → API)
# -----------------------------
@app.post("/ingest/heartbeat")
def ingest_heartbeat():
    payload = request.json or {}
    payload["time_utc"] = utc_now()

    with open(HEARTBEAT_FILE, "w") as f:
        json.dump(payload, f, indent=2)

    return jsonify({"status": "ok"})

@app.post("/ingest/trade")
def ingest_trade():
    d = request.json or {}

    append_csv(TRADES_FILE, [
        d.get("entry_time"),
        d.get("exit_time"),
        d.get("hold_minutes"),
        d.get("market"),
        d.get("entry_price"),
        d.get("exit_price"),
        d.get("qty"),
        d.get("pnl_usd"),
        d.get("pnl_pct"),
        d.get("take_profit_pct"),
        d.get("stop_loss_pct"),
        d.get("risk_mode"),
        d.get("trend_strength"),
        d.get("rsi"),
        d.get("volatility"),
    ])

    return jsonify({"status": "ok"})

@app.post("/ingest/equity")
def ingest_equity():
    d = request.json or {}

    append_csv(EQUITY_FILE, [
        d.get("time_utc", utc_now()),
        d.get("equity_usd")
    ])

    return jsonify({"status": "ok"})

@app.post("/ingest/training")
def ingest_training():
    d = request.json or {}

    append_csv(TRAINING_FILE, [
        d.get("time_utc", utc_now()),
        d.get("event"),
        json.dumps(d.get("details", {}))
    ])

    return jsonify({"status": "ok"})

# -----------------------------
# Dashboard API
# -----------------------------
@app.get("/data")
def dashboard_data():
    trades = read_csv(TRADES_FILE)
    equity_rows = read_csv(EQUITY_FILE)

    total_trades = len(trades)
    wins = 0
    losses = 0
    pnl_total = 0.0
    pnl_wins_sum = 0.0
    pnl_losses_sum = 0.0

    best_trade = None
    worst_trade = None

    per_market = {}
    recent_trades = []

    for t in trades[-20:][::-1]:
        pnl = to_float(t.get("pnl_usd"))
        market = t.get("market", "UNKNOWN")

        pnl_total += pnl
        if pnl >= 0:
            wins += 1
            pnl_wins_sum += pnl
        else:
            losses += 1
            pnl_losses_sum += pnl

        best_trade = pnl if best_trade is None else max(best_trade, pnl)
        worst_trade = pnl if worst_trade is None else min(worst_trade, pnl)

        if market not in per_market:
            per_market[market] = {"trades": 0, "wins": 0, "total_pnl": 0.0}

        per_market[market]["trades"] += 1
        per_market[market]["total_pnl"] += pnl
        if pnl >= 0:
            per_market[market]["wins"] += 1

        recent_trades.append({
            "time": t.get("exit_time"),
            "market": market,
            "pnl_usd": pnl
        })

    win_rate = (wins / total_trades * 100.0) if total_trades else 0.0

    markets = []
    for m, v in per_market.items():
        wr = (v["wins"] / v["trades"] * 100.0) if v["trades"] else 0.0
        markets.append({
            "market": m,
            "trades": v["trades"],
            "win_rate": wr,
            "total_pnl": v["total_pnl"],
            "avg_pnl": (v["total_pnl"] / v["trades"]) if v["trades"] else 0.0
        })

    markets.sort(key=lambda x: x["total_pnl"], reverse=True)

    equity_curve = []
    equity_vals = []

    for r in equity_rows:
        e = to_float(r.get("equity_usd"), None)
        if e is None:
            continue
        equity_curve.append({
            "time_utc": r.get("time_utc"),
            "equity_usd": e
        })
        equity_vals.append(e)

    max_drawdown = 0.0
    if equity_vals:
        peak = equity_vals[0]
        for v in equity_vals:
            peak = max(peak, v)
            max_drawdown = max(max_drawdown, peak - v)

    heartbeat = {}
    status = "unknown"
    last_heartbeat = None

    if os.path.exists(HEARTBEAT_FILE):
        with open(HEARTBEAT_FILE) as f:
            heartbeat = json.load(f)
            status = heartbeat.get("status", "unknown")
            last_heartbeat = heartbeat.get("time_utc")

    return jsonify({
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl_usd": pnl_total,
        "bot_status": {
            "status": status,
            "last_heartbeat": last_heartbeat
        },
        "advanced_metrics": {
            "profit_factor": (pnl_wins_sum / abs(pnl_losses_sum)) if pnl_losses_sum < 0 else None,
            "avg_win_usd": (pnl_wins_sum / wins) if wins else 0.0,
            "avg_loss_usd": abs((pnl_losses_sum / losses)) if losses else 0.0,
            "best_trade_usd": best_trade,
            "worst_trade_usd": worst_trade,
            "max_drawdown_usd": max_drawdown,
            "recovery_factor": None,
            "sharpe_ratio": None
        },
        "equity_curve": equity_curve,
        "per_market": markets,
        "recent_trades": recent_trades,
        "total_events": len(read_csv(TRAINING_FILE))
    })

# -----------------------------
# Static frontend
# -----------------------------
@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")

@app.get("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)

# -----------------------------
# Run
# -----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

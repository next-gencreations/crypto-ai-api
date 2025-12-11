import os
import json
from datetime import datetime, timezone
from flask import Flask, jsonify, send_from_directory

app = Flask(__name__, static_folder="static")

# Data folder (Render disk or local)
DATA_DIR = os.environ.get("DATA_DIR", "/opt/render/project/src/data")

TRADES_FILE = os.path.join(DATA_DIR, "trades.csv")
EQUITY_FILE = os.path.join(DATA_DIR, "equity_curve.csv")
TRAINING_FILE = os.path.join(DATA_DIR, "training_events.csv")
HEARTBEAT_FILE = os.path.join(DATA_DIR, "heartbeat.json")


def _safe_read_json(path: str):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


@app.get("/")
def home():
    # Serve the dashboard HTML directly (NOT Jinja templates)
    return send_from_directory(app.static_folder, "index.html")


@app.get("/health")
def health():
    return jsonify({"ok": True, "time_utc": datetime.now(timezone.utc).isoformat()})


@app.get("/data")
def data():
    """
    Minimal API for the dashboard.
    For now it returns safe defaults so the UI never breaks,
    and it can grow as your bot writes more CSV data over time.
    """
    hb = _safe_read_json(HEARTBEAT_FILE) or {}
    status = hb.get("status", "unknown")
    last_heartbeat = hb.get("time_utc")
    minutes_since = hb.get("minutes_since")

    payload = {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "total_pnl_usd": 0.0,
        "bot_status": {
            "status": status,
            "last_heartbeat": last_heartbeat,
            "minutes_since": minutes_since,
        },
        "advanced_metrics": {
            "profit_factor": None,
            "avg_win_usd": 0.0,
            "avg_loss_usd": 0.0,
            "best_trade_usd": 0.0,
            "worst_trade_usd": 0.0,
            "max_drawdown_usd": 0.0,
            "recovery_factor": None,
            "sharpe_ratio": None,
        },
        "equity_curve": [],
        "per_market": [],
        "recent_trades": [],
        "total_events": 0,
    }

    return jsonify(payload)


# Serve any other static files (css/js/images)
@app.get("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(app.static_folder, filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

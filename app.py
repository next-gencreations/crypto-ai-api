from flask import Flask, jsonify, send_from_directory, request
import csv
import os
from datetime import datetime

app = Flask(__name__, static_folder="static")

# Where we store training / trade events sent from the bot
DATA_DIR = "data"
DATA_FILE = os.path.join(DATA_DIR, "training_events.csv")

# For equity curve reconstruction
START_BALANCE_USD = 1000.0


def ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


# ---------------------------------------------------------------------
# Root → serve dashboard HTML
# ---------------------------------------------------------------------
@app.route("/")
def index():
    # index.html lives in the "static" folder
    return send_from_directory("static", "index.html")


# ---------------------------------------------------------------------
# Simple health check
# ---------------------------------------------------------------------
@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------
# Aggregated stats for dashboard
# ---------------------------------------------------------------------
@app.route("/data")
def get_data():
    """
    Returns:
    {
      total_events, wins, losses, total_pnl_usd, avg_pnl_usd, win_rate,
      equity_curve: [{time, equity_usd}, ...],
      recent_trades: [{time, market, pnl_usd}, ...],
      per_market: [{
         market, trades, wins, losses, win_rate, total_pnl_usd, avg_pnl_usd
      }, ...]
    }
    """
    ensure_data_dir()

    stats = {
        "total_events": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl_usd": 0.0,
        "avg_pnl_usd": 0.0,
        "win_rate": 0.0,
        "equity_curve": [],
        "recent_trades": [],
        "per_market": [],
    }

    if not os.path.exists(DATA_FILE):
        return jsonify(stats)

    rows = []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    if not rows:
        return jsonify(stats)

    # Helper to parse ISO times safely
    def parse_time(s: str):
        if not s:
            return None
        try:
            # Handle both "...Z" and normal isoformat
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s)
        except Exception:
            return None

    # Sort by exit_time (or entry_time as fallback)
    rows.sort(
        key=lambda r: parse_time(r.get("exit_time") or r.get("entry_time") or "") or datetime.min
    )

    equity = START_BALANCE_USD
    equity_curve = []
    per_market = {}

    for r in rows:
        try:
            pnl = float(r.get("pnl_usd", 0.0))
        except (TypeError, ValueError):
            pnl = 0.0

        market = r.get("market", "UNKNOWN")

        stats["total_events"] += 1
        stats["total_pnl_usd"] += pnl

        if pnl >= 0:
            stats["wins"] += 1
        else:
            stats["losses"] += 1

        # Equity curve
        equity += pnl
        time_str = r.get("exit_time") or r.get("entry_time") or ""
        equity_curve.append({"time": time_str, "equity_usd": equity})

        # Per-market breakdown
        m = per_market.setdefault(
            market,
            {
                "market": market,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl_usd": 0.0,
            },
        )
        m["trades"] += 1
        m["total_pnl_usd"] += pnl
        if pnl >= 0:
            m["wins"] += 1
        else:
            m["losses"] += 1

    # Final overall stats
    if stats["total_events"] > 0:
        stats["avg_pnl_usd"] = stats["total_pnl_usd"] / stats["total_events"]
        stats["win_rate"] = stats["wins"] / stats["total_events"]

    # Per-market: compute win_rate & avg_pnl
    per_market_list = []
    for m in per_market.values():
        trades = m["trades"]
        win_rate = (m["wins"] / trades) if trades else 0.0
        avg_pnl = (m["total_pnl_usd"] / trades) if trades else 0.0
        per_market_list.append(
            {
                "market": m["market"],
                "trades": trades,
                "wins": m["wins"],
                "losses": m["losses"],
                "win_rate": win_rate,
                "total_pnl_usd": m["total_pnl_usd"],
                "avg_pnl_usd": avg_pnl,
            }
        )

    stats["equity_curve"] = equity_curve
    stats["per_market"] = per_market_list

    # Recent trades = last 20 events
    recent = []
    for r in rows[-20:]:
        try:
            pnl = float(r.get("pnl_usd", 0.0))
        except (TypeError, ValueError):
            pnl = 0.0
        recent.append(
            {
                "time": r.get("exit_time") or r.get("entry_time") or "",
                "market": r.get("market", "UNKNOWN"),
                "pnl_usd": pnl,
            }
        )
    stats["recent_trades"] = recent

    return jsonify(stats)


# ---------------------------------------------------------------------
# Endpoint the bot calls to send training / trade events
# ---------------------------------------------------------------------
@app.route("/training-events", methods=["POST"])
def training_events():
    """
    Called by the bot (Crypto-AI-Bot) after each closed trade.
    Expects JSON with keys like:
    entry_time, exit_time, hold_minutes, market, trend_strength, rsi,
    volatility, entry_price, exit_price, pnl_usd, pnl_pct,
    take_profit_pct, stop_loss_pct, risk_mode
    """
    ensure_data_dir()

    event = request.get_json(silent=True)
    if not isinstance(event, dict):
        return jsonify({"error": "Invalid JSON payload"}), 400

    fieldnames = [
        "entry_time",
        "exit_time",
        "hold_minutes",
        "market",
        "trend_strength",
        "rsi",
        "volatility",
        "entry_price",
        "exit_price",
        "pnl_usd",
        "pnl_pct",
        "take_profit_pct",
        "stop_loss_pct",
        "risk_mode",
    ]

    file_exists = os.path.exists(DATA_FILE)

    with open(DATA_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        # Only keep the known fields, ignore extras
        clean_row = {k: event.get(k, "") for k in fieldnames}
        writer.writerow(clean_row)

    return jsonify({"status": "ok"})


if __name__ == "__main__":
    # For local testing; on Render we use gunicorn
    app.run(host="0.0.0.0", port=5000, debug=False)

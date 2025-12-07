from flask import Flask, jsonify, request
import pandas as pd
from decimal import Decimal, getcontext
from pathlib import Path
import csv
from datetime import datetime

app = Flask(__name__)

# High precision for money maths
getcontext().prec = 28

DATA_FILE = Path("data/training_events.csv")


def ensure_data_dir():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_data():
    """Loads training data from CSV and returns DataFrame"""
    if not DATA_FILE.exists():
        return None, "training_events.csv not found"

    try:
        df = pd.read_csv(DATA_FILE)
        return df, None
    except Exception as e:
        return None, str(e)


def compute_summary(df: pd.DataFrame):
    """Compute basic performance stats from the dataframe."""

    wins = 0
    losses = 0
    total_pnl = Decimal("0")

    for _, row in df.iterrows():
        try:
            pnl = Decimal(str(row.get("pnl_usd", "0")))
        except Exception:
            pnl = Decimal("0")

        total_pnl += pnl

        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

    total_trades = len(df)
    realised = wins + losses if (wins + losses) > 0 else 1

    win_rate = float(Decimal(wins) / Decimal(realised))

    summary = {
        "total_events": int(total_trades),
        "wins": int(wins),
        "losses": int(losses),
        "win_rate": win_rate,
        "total_pnl_usd": float(total_pnl),
        "avg_pnl_usd": float(total_pnl / Decimal(realised)),
    }

    return summary


@app.route("/")
def home():
    return "Crypto AI API Running 🚀"


@app.route("/data")
def data():
    """Return summary stats from the CSV."""
    df, error = load_data()

    if error:
        return jsonify({"error": error}), 500

    summary = compute_summary(df)
    return jsonify(summary), 200


@app.route("/trade", methods=["POST"])
def record_trade():
    """
    Bot calls this when a trade closes.
    We append one row to training_events.csv and return updated summary.
    """
    ensure_data_dir()

    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    # Minimal required fields (this matches your CSV header)
    required_fields = [
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

    missing = [f for f in required_fields if f not in payload]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    # If times are Python datetimes on the bot side, they should be sent as ISO strings.
    # Here we just trust they are strings.
    row = {field: payload[field] for field in required_fields}

    # Append to CSV (create file with header if needed)
    file_exists = DATA_FILE.exists()

    with DATA_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=required_fields)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    # Reload data and return fresh summary
    df, error = load_data()
    if error:
        return jsonify({"error": error}), 500

    summary = compute_summary(df)
    return jsonify({"status": "ok", "summary": summary}), 201


# Required for Render local run; in production Render uses gunicorn.
if __name__ == "__main__":
    ensure_data_dir()
    app.run(host="0.0.0.0", port=10000)

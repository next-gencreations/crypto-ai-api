from flask import Flask, jsonify
import pandas as pd
from decimal import Decimal, getcontext
from pathlib import Path

app = Flask(__name__)

# Set decimal precision for calculations
getcontext().prec = 28

# Path to your CSV file
DATA_FILE = Path("data/training_events.csv")


def load_data():
    """Loads training data from CSV and returns DataFrame"""
    if not DATA_FILE.exists():
        return None, "training_events.csv not found"

    try:
        df = pd.read_csv(DATA_FILE)
        return df, None
    except Exception as e:
        return None, str(e)


def compute_summary(df):
    """Compute summary statistics"""

    wins = 0
    losses = 0

    for _, row in df.iterrows():
        try:
            pnl = Decimal(str(row.get("pnl_usd", "0")))
        except:
            pnl = Decimal("0")

        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

    total = wins + losses if wins + losses > 0 else 1

    summary = {
        "total_events": int(len(df)),
        "wins": int(wins),
        "losses": int(losses),
        "win_rate": float(wins / total),
    }

    return summary


@app.route("/")
def home():
    return "Crypto AI API Running 🚀"


@app.route("/data")
def data():
    df, error = load_data()

    if error:
        return jsonify({"error": error}), 500

    summary = compute_summary(df)
    return jsonify(summary), 200


# Required for Render
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

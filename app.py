from flask import Flask, jsonify, send_from_directory
import csv
import os

app = Flask(__name__, static_folder="static")

DATA_FILE = "data/training_events.csv"

# --------------------------
# Root → Dashboard HTML
# --------------------------
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

# --------------------------
# Health Check
# --------------------------
@app.route("/health")
def health():
    return jsonify({"status": "ok"})

# --------------------------
# Raw training stats
# --------------------------
@app.route("/data")
def get_data():
    stats = {
        "total_events": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl_usd": 0.0,
    }

    if not os.path.exists(DATA_FILE):
        return jsonify(stats)

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            stats["total_events"] += 1

            pnl = float(row.get("pnl_usd", 0))
            stats["total_pnl_usd"] += pnl

            if pnl > 0:
                stats["wins"] += 1
            else:
                stats["losses"] += 1

    if stats["total_events"] > 0:
        stats["avg_pnl_usd"] = stats["total_pnl_usd"] / stats["total_events"]
        stats["win_rate"] = stats["wins"] / stats["total_events"]
    else:
        stats["avg_pnl_usd"] = 0
        stats["win_rate"] = 0

    return jsonify(stats)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

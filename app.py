from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import json
from datetime import datetime

app = Flask(__name__)
CORS(app)

# =========================
# Disk-backed storage
# =========================
DATA_DIR = os.environ.get("DATA_DIR", "/var/data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

os.makedirs(DATA_DIR, exist_ok=True)

# =========================
# Helpers
# =========================
def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return {}
    try:
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_settings(data):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)

# =========================
# Health
# =========================
@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        ok=True,
        service="crypto-ai-api",
        time_utc=datetime.utcnow().isoformat() + "Z",
        vault_enabled=True,
        vault_unlocked=False,
    )

# =========================
# Settings (THIS FIXES 500)
# =========================
@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "GET":
        return jsonify(load_settings())

    try:
        payload = request.get_json(force=True, silent=True)
        if not isinstance(payload, dict):
            return jsonify(ok=False, error="invalid_payload"), 400

        current = load_settings()
        current.update(payload)
        save_settings(current)

        return jsonify(ok=True, settings=current)

    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

# =========================
# Root
# =========================
@app.route("/", methods=["GET"])
def root():
    return jsonify(ok=True, service="crypto-ai-api")

# =========================
# Entrypoint
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

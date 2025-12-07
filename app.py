from flask import Flask, send_file
import os

app = Flask(__name__)

DATA_FILE = "/opt/render/project/src/data/training_events.csv"

@app.route("/")
def home():
    return "Crypto AI API Running 🚀"

@app.route("/download/events")
def download_events():
    if os.path.exists(DATA_FILE):
        return send_file(DATA_FILE, as_attachment=True)
    else:
        return {"error": "training_events.csv not found"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

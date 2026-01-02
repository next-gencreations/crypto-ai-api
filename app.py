import os
import sqlite3
from datetime import datetime, timezone
from flask import Flask, jsonify, request
from flask_cors import CORS

# =========================
# App & CORS
# =========================
app = Flask(__name__)
CORS(app)  # <-- THIS FIXES VERCEL / BROWSER CORS

# =========================
# Database
# =========================
DATA_DIR = "/var/data"
DB_PATH = os.path.join(DATA_DIR, "data.db")

os.makedirs(DATA_DIR, exist_ok=True)

def db():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def now():
    return datetime.now(timezone.utc).isoformat()

# =========================
# Init DB
# =========================
def init_db():
    conn = db()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS control (
        id INTEGER PRIMARY KEY,
        paused INTEGER,
        pause_reason TEXT,
        pause_until_utc TEXT,
        updated_time_utc TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS heartbeat (
        id INTEGER PRIMARY KEY,
        equity_usd REAL,
        losses INTEGER,
        wins INTEGER,
        total_trades INTEGER,
        open_positions INTEGER,
        markets TEXT,
        prices_ok INTEGER,
        survival_mode TEXT,
        time_utc TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS pet (
        id INTEGER PRIMARY KEY,
        stage TEXT,
        mood TEXT,
        health REAL,
        hunger REAL,
        growth REAL,
        survival_mode TEXT,
        time_utc TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market TEXT,
        side TEXT,
        pnl_usd REAL,
        price REAL,
        confidence REAL,
        reason TEXT,
        time_utc TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS equity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        equity_usd REAL,
        time_utc TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS deaths (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reason TEXT,
        equity_usd REAL,
        time_utc TEXT
    )""")

    # ensure control row exists
    c.execute("SELECT COUNT(*) FROM control")
    if c.fetchone()[0] == 0:
        c.execute(
            "INSERT INTO control VALUES (1, 0, '', '', ?)",
            (now(),)
        )

    conn.commit()
    conn.close()

init_db()

# =========================
# Helpers
# =========================
def fetch_all(table, limit=100):
    conn = db()
    c = conn.cursor()
    c.execute(f"SELECT * FROM {table} ORDER BY time_utc DESC LIMIT ?", (limit,))
    cols = [d[0] for d in c.description]
    rows = [dict(zip(cols, r)) for r in c.fetchall()]
    conn.close()
    return rows[::-1]

def fetch_one(table):
    conn = db()
    c = conn.cursor()
    c.execute(f"SELECT * FROM {table} ORDER BY time_utc DESC LIMIT 1")
    row = c.fetchone()
    if not row:
        return {}
    cols = [d[0] for d in c.description]
    conn.close()
    return dict(zip(cols, row))

# =========================
# Root
# =========================
@app.route("/")
def root():
    return jsonify({
        "service": "crypto-ai-api",
        "ok": True,
        "db_parent_exists": os.path.exists(DATA_DIR),
        "db_path": DB_PATH,
        "time_utc": now()
    })

# =========================
# Read Endpoints (Dashboard)
# =========================
@app.route("/data")
def data():
    return jsonify({
        "control": fetch_one("control"),
        "heartbeat": fetch_one("heartbeat"),
        "pet": fetch_one("pet"),
        "equity": fetch_all("equity", 50),
        "trades": fetch_all("trades", 50),
        "deaths": fetch_all("deaths", 50),
        "events": []
    })

@app.route("/heartbeat")
def heartbeat():
    return jsonify(fetch_one("heartbeat"))

@app.route("/pet")
def pet():
    return jsonify(fetch_one("pet"))

@app.route("/trades")
def trades():
    return jsonify(fetch_all("trades", 100))

@app.route("/equity")
def equity():
    return jsonify(fetch_all("equity", 100))

@app.route("/deaths")
def deaths():
    return jsonify(fetch_all("deaths", 100))

# =========================
# Ingest Endpoints (Bot)
# =========================
@app.route("/ingest/heartbeat", methods=["POST"])
def ingest_heartbeat():
    d = request.json or {}
    conn = db()
    c = conn.cursor()
    c.execute("""
    INSERT INTO heartbeat VALUES (
        ?,?,?,?,?,?,?,?,?
    )""", (
        1,
        d.get("equity_usd", 0),
        d.get("losses", 0),
        d.get("wins", 0),
        d.get("total_trades", 0),
        d.get("open_positions", 0),
        ",".join(d.get("markets", [])),
        int(d.get("prices_ok", True)),
        d.get("survival_mode", "NORMAL"),
        now()
    ))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/ingest/pet", methods=["POST"])
def ingest_pet():
    d = request.json or {}
    conn = db()
    c = conn.cursor()
    c.execute("""
    INSERT OR REPLACE INTO pet VALUES (
        1,?,?,?,?,?,?
    )""", (
        d.get("stage", "egg"),
        d.get("mood", "neutral"),
        d.get("health", 100),
        d.get("hunger", 0),
        d.get("growth", 0),
        d.get("survival_mode", "NORMAL"),
        now()
    ))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/ingest/trade", methods=["POST"])
def ingest_trade():
    d = request.json or {}
    conn = db()
    c = conn.cursor()
    c.execute("""
    INSERT INTO trades (
        market, side, pnl_usd, price, confidence, reason, time_utc
    ) VALUES (?,?,?,?,?,?,?)
    """, (
        d.get("market"),
        d.get("side"),
        d.get("pnl_usd", 0),
        d.get("price", 0),
        d.get("confidence", 0),
        d.get("reason", ""),
        now()
    ))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/ingest/equity", methods=["POST"])
def ingest_equity():
    d = request.json or {}
    conn = db()
    c = conn.cursor()
    c.execute("""
    INSERT INTO equity (equity_usd, time_utc)
    VALUES (?,?)
    """, (d.get("equity_usd", 0), now()))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/ingest/death", methods=["POST"])
def ingest_death():
    d = request.json or {}
    conn = db()
    c = conn.cursor()
    c.execute("""
    INSERT INTO deaths (reason, equity_usd, time_utc)
    VALUES (?,?,?)
    """, (d.get("reason", "unknown"), d.get("equity_usd", 0), now()))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# =========================
# Control
# =========================
@app.route("/control/pause", methods=["POST"])
def pause():
    d = request.json or {}
    conn = db()
    c = conn.cursor()
    c.execute("""
    UPDATE control SET paused=1, pause_reason=?, pause_until_utc=?, updated_time_utc=?
    WHERE id=1
    """, (d.get("reason", ""), d.get("until_utc", ""), now()))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/control/revive", methods=["POST"])
def revive():
    conn = db()
    c = conn.cursor()
    c.execute("""
    UPDATE control SET paused=0, pause_reason='', pause_until_utc='', updated_time_utc=?
    WHERE id=1
    """, (now(),))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# =========================
# Run (local only)
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

import os
import json
import sqlite3
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from flask_cors import CORS

# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
# IMPORTANT:
# On Render, attach a disk to THIS API service at /var/data
# Then set DB_PATH environment variable to: /var/data/data.db
DB_PATH = os.environ.get("DB_PATH", "data.db")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    # heartbeat = single row
    cur.execute("""
    CREATE TABLE IF NOT EXISTS heartbeat (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        time_utc TEXT,
        status TEXT,
        survival_mode TEXT,
        equity_usd REAL,
        open_positions INTEGER,
        prices_ok INTEGER,
        markets TEXT,
        losses INTEGER,
        total_trades INTEGER,
        wins INTEGER,
        total_pnl_usd REAL
    )
    """)

    # pet = single row
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pet (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        time_utc TEXT,
        stage TEXT,
        mood TEXT,
        health REAL,
        hunger REAL,
        growth REAL,
        fainted_until_utc TEXT,
        survival_mode TEXT
    )
    """)

    # events = list
    cur.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time_utc TEXT,
        type TEXT,
        message TEXT,
        details TEXT
    )
    """)

    # equity timeline
    cur.execute("""
    CREATE TABLE IF NOT EXISTS equity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time_utc TEXT,
        equity_usd REAL
    )
    """)

    # trades list
    cur.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time_utc TEXT,
        market TEXT,
        side TEXT,
        size_usd REAL,
        price REAL,
        pnl_usd REAL,
        reason TEXT,
        confidence REAL
    )
    """)

    # prices snapshot (single row)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS prices (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        time_utc TEXT,
        json TEXT
    )
    """)

    # NEW: deaths (crash/death log)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS deaths (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time_utc TEXT,
        source TEXT,
        reason TEXT,
        details TEXT
    )
    """)

    # NEW: control (single row) - allows pausing/revive from API
    cur.execute("""
    CREATE TABLE IF NOT EXISTS control (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        pause_until_utc TEXT,
        pause_reason TEXT,
        updated_time_utc TEXT
    )
    """)

    # ensure control row exists
    cur.execute("""
        INSERT INTO control (id, pause_until_utc, pause_reason, updated_time_utc)
        VALUES (1, '', '', ?)
        ON CONFLICT(id) DO NOTHING
    """, (utc_now_iso(),))

    conn.commit()
    conn.close()


init_db()

# -----------------------------------------------------------------------------
# Helpers: json safe load
# -----------------------------------------------------------------------------
def _safe_json_loads(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None

# -----------------------------------------------------------------------------
# Helpers: read state
# -----------------------------------------------------------------------------
def get_heartbeat():
    conn = db()
    row = conn.execute("SELECT * FROM heartbeat WHERE id=1").fetchone()
    conn.close()
    return dict(row) if row else None


def get_pet():
    conn = db()
    row = conn.execute("SELECT * FROM pet WHERE id=1").fetchone()
    conn.close()
    return dict(row) if row else None


def get_events(limit=200):
    conn = db()
    rows = conn.execute(
        "SELECT time_utc, type, message, details FROM events ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "time_utc": r["time_utc"],
            "type": r["type"],
            "message": r["message"],
            "details": _safe_json_loads(r["details"])
        })
    return list(reversed(out))


def get_equity(limit=500):
    conn = db()
    rows = conn.execute(
        "SELECT time_utc, equity_usd FROM equity ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    out = [{"time_utc": r["time_utc"], "equity_usd": r["equity_usd"]} for r in rows]
    return list(reversed(out))


def get_trades(limit=200):
    conn = db()
    rows = conn.execute(
        "SELECT time_utc, market, side, size_usd, price, pnl_usd, reason, confidence FROM trades ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "time_utc": r["time_utc"],
            "market": r["market"],
            "side": r["side"],
            "size_usd": r["size_usd"],
            "price": r["price"],
            "pnl_usd": r["pnl_usd"],
            "reason": r["reason"],
            "confidence": r["confidence"],
        })
    return list(reversed(out))


def get_prices():
    conn = db()
    row = conn.execute("SELECT json FROM prices WHERE id=1").fetchone()
    conn.close()
    if not row or not row["json"]:
        return {}
    data = _safe_json_loads(row["json"])
    return data if isinstance(data, dict) else {}


def get_deaths(limit=200):
    conn = db()
    rows = conn.execute(
        "SELECT time_utc, source, reason, details FROM deaths ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "time_utc": r["time_utc"],
            "source": r["source"],
            "reason": r["reason"],
            "details": _safe_json_loads(r["details"])
        })
    return list(reversed(out))


def get_control():
    conn = db()
    row = conn.execute("SELECT * FROM control WHERE id=1").fetchone()
    conn.close()
    return dict(row) if row else {"pause_until_utc": "", "pause_reason": "", "updated_time_utc": utc_now_iso()}


def is_paused_now():
    c = get_control()
    pause_until = c.get("pause_until_utc") or ""
    if not pause_until:
        return False, None, None
    try:
        # parse isoformat
        dt = datetime.fromisoformat(pause_until)
        now = datetime.now(timezone.utc)
        return dt > now, pause_until, c.get("pause_reason", "")
    except Exception:
        return False, pause_until, c.get("pause_reason", "")

# -----------------------------------------------------------------------------
# Stats (simple computed summary)
# -----------------------------------------------------------------------------
def _stats():
    hb = get_heartbeat() or {}
    conn = db()

    row = conn.execute("""
        SELECT
            COUNT(*) AS total_trades,
            COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END),0) AS wins,
            COALESCE(SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END),0) AS losses,
            COALESCE(SUM(pnl_usd),0) AS total_pnl_usd
        FROM trades
    """).fetchone()

    drow = conn.execute("""
        SELECT COUNT(*) AS total_deaths
        FROM deaths
    """).fetchone()

    conn.close()

    total_trades = int(row["total_trades"] or 0)
    wins = int(row["wins"] or 0)
    losses = int(row["losses"] or 0)
    total_pnl_usd = float(row["total_pnl_usd"] or 0.0)
    win_rate = (wins / total_trades) if total_trades > 0 else 0.0
    avg_pnl = (total_pnl_usd / total_trades) if total_trades > 0 else 0.0

    total_deaths = int(drow["total_deaths"] or 0)

    paused, pause_until, pause_reason = is_paused_now()

    return {
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl_usd": total_pnl_usd,
        "avg_pnl": avg_pnl,
        "equity_usd": hb.get("equity_usd", None),
        "total_deaths": total_deaths,
        "paused": paused,
        "pause_until_utc": pause_until or "",
        "pause_reason": pause_reason or "",
    }

# -----------------------------------------------------------------------------
# Public GET endpoints (for browser/dashboard)
# -----------------------------------------------------------------------------
@app.get("/")
def root():
    return jsonify({
        "ok": True,
        "service": "crypto-ai-api",
        "time_utc": utc_now_iso(),
        "endpoints": {
            "GET": ["/data", "/heartbeat", "/pet", "/events", "/equity", "/trades", "/prices", "/deaths", "/control"],
            "POST": [
                "/ingest/heartbeat", "/ingest/pet", "/ingest/event", "/ingest/equity", "/ingest/trade", "/ingest/prices",
                "/ingest/death", "/control/pause", "/control/revive"
            ],
            "DELETE": ["/reset/all", "/reset/events", "/reset/trades", "/reset/equity", "/reset/deaths"]
        }
    })


@app.get("/data")
def data():
    return jsonify({
        "equity": get_equity(),
        "events": get_events(),
        "heartbeat": get_heartbeat(),
        "pet": get_pet(),
        "stats": _stats(),
        "trades": get_trades(),
        "prices": get_prices(),
        "deaths": get_deaths(),
        "control": get_control()
    })


@app.get("/heartbeat")
def heartbeat_get():
    return jsonify(get_heartbeat() or {})


@app.get("/pet")
def pet_get():
    return jsonify(get_pet() or {})


@app.get("/events")
def events_get():
    limit = int(request.args.get("limit", "200"))
    return jsonify(get_events(limit=limit))


@app.get("/equity")
def equity_get():
    limit = int(request.args.get("limit", "500"))
    return jsonify(get_equity(limit=limit))


@app.get("/trades")
def trades_get():
    limit = int(request.args.get("limit", "200"))
    return jsonify(get_trades(limit=limit))


@app.get("/prices")
def prices_get():
    return jsonify(get_prices())


@app.get("/deaths")
def deaths_get():
    limit = int(request.args.get("limit", "200"))
    return jsonify(get_deaths(limit=limit))


@app.get("/control")
def control_get():
    return jsonify(get_control())

# -----------------------------------------------------------------------------
# Ingest POST endpoints (for the bot/worker)
# -----------------------------------------------------------------------------
@app.post("/ingest/heartbeat")
def ingest_heartbeat():
    payload = request.get_json(force=True, silent=True) or {}
    conn = db()
    conn.execute("""
        INSERT INTO heartbeat (id, time_utc, status, survival_mode, equity_usd, open_positions, prices_ok, markets, losses, total_trades, wins, total_pnl_usd)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          time_utc=excluded.time_utc,
          status=excluded.status,
          survival_mode=excluded.survival_mode,
          equity_usd=excluded.equity_usd,
          open_positions=excluded.open_positions,
          prices_ok=excluded.prices_ok,
          markets=excluded.markets,
          losses=excluded.losses,
          total_trades=excluded.total_trades,
          wins=excluded.wins,
          total_pnl_usd=excluded.total_pnl_usd
    """, (
        payload.get("time_utc", utc_now_iso()),
        payload.get("status", "running"),
        payload.get("survival_mode", "NORMAL"),
        payload.get("equity_usd", 0.0),
        payload.get("open_positions", 0),
        1 if payload.get("prices_ok", True) else 0,
        json.dumps(payload.get("markets", [])),
        payload.get("losses", 0),
        payload.get("total_trades", 0),
        payload.get("wins", 0),
        payload.get("total_pnl_usd", 0.0),
    ))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.post("/ingest/pet")
def ingest_pet():
    payload = request.get_json(force=True, silent=True) or {}
    conn = db()
    conn.execute("""
        INSERT INTO pet (id, time_utc, stage, mood, health, hunger, growth, fainted_until_utc, survival_mode)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          time_utc=excluded.time_utc,
          stage=excluded.stage,
          mood=excluded.mood,
          health=excluded.health,
          hunger=excluded.hunger,
          growth=excluded.growth,
          fainted_until_utc=excluded.fainted_until_utc,
          survival_mode=excluded.survival_mode
    """, (
        payload.get("time_utc", utc_now_iso()),
        payload.get("stage", "egg"),
        payload.get("mood", "focused"),
        payload.get("health", 100.0),
        payload.get("hunger", 50.0),
        payload.get("growth", 0.0),
        payload.get("fainted_until_utc", ""),
        payload.get("survival_mode", "NORMAL"),
    ))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.post("/ingest/event")
def ingest_event():
    payload = request.get_json(force=True, silent=True) or {}
    conn = db()
    conn.execute("""
        INSERT INTO events (time_utc, type, message, details)
        VALUES (?, ?, ?, ?)
    """, (
        payload.get("time_utc", utc_now_iso()),
        payload.get("type", "info"),
        payload.get("message", ""),
        json.dumps(payload.get("details", {}))
    ))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.post("/ingest/equity")
def ingest_equity():
    payload = request.get_json(force=True, silent=True) or {}
    conn = db()
    conn.execute("""
        INSERT INTO equity (time_utc, equity_usd)
        VALUES (?, ?)
    """, (
        payload.get("time_utc", utc_now_iso()),
        float(payload.get("equity_usd", 0.0))
    ))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.post("/ingest/trade")
def ingest_trade():
    payload = request.get_json(force=True, silent=True) or {}
    conn = db()
    conn.execute("""
        INSERT INTO trades (time_utc, market, side, size_usd, price, pnl_usd, reason, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        payload.get("time_utc", utc_now_iso()),
        payload.get("market", ""),
        payload.get("side", ""),
        float(payload.get("size_usd", 0.0)),
        float(payload.get("price", 0.0)),
        float(payload.get("pnl_usd", 0.0)),
        payload.get("reason", ""),
        float(payload.get("confidence", 0.0)),
    ))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.post("/ingest/prices")
def ingest_prices():
    payload = request.get_json(force=True, silent=True) or {}
    conn = db()
    conn.execute("""
        INSERT INTO prices (id, time_utc, json)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          time_utc=excluded.time_utc,
          json=excluded.json
    """, (
        payload.get("time_utc", utc_now_iso()),
        json.dumps(payload.get("prices", payload))
    ))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# NEW: ingest death record
@app.post("/ingest/death")
def ingest_death():
    payload = request.get_json(force=True, silent=True) or {}
    conn = db()
    conn.execute("""
        INSERT INTO deaths (time_utc, source, reason, details)
        VALUES (?, ?, ?, ?)
    """, (
        payload.get("time_utc", utc_now_iso()),
        payload.get("source", "bot"),
        payload.get("reason", ""),
        json.dumps(payload.get("details", {}))
    ))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# -----------------------------------------------------------------------------
# Control endpoints (API tells bot to pause/revive)
# -----------------------------------------------------------------------------
@app.post("/control/pause")
def control_pause():
    payload = request.get_json(force=True, silent=True) or {}
    seconds = int(payload.get("seconds", 600))  # default 10 mins
    reason = payload.get("reason", "pause requested")

    now = datetime.now(timezone.utc)
    pause_until = (now.replace(microsecond=0) + __import__("datetime").timedelta(seconds=seconds)).isoformat()

    conn = db()
    conn.execute("""
        INSERT INTO control (id, pause_until_utc, pause_reason, updated_time_utc)
        VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          pause_until_utc=excluded.pause_until_utc,
          pause_reason=excluded.pause_reason,
          updated_time_utc=excluded.updated_time_utc
    """, (pause_until, reason, utc_now_iso()))
    conn.commit()
    conn.close()

    # also create an event
    _add_event("warning", f"Pause set for {seconds}s", {"pause_until_utc": pause_until, "reason": reason})

    return jsonify({"ok": True, "pause_until_utc": pause_until, "seconds": seconds, "reason": reason})


@app.post("/control/revive")
def control_revive():
    payload = request.get_json(force=True, silent=True) or {}
    reason = payload.get("reason", "revive requested")

    # clear pause
    conn = db()
    conn.execute("""
        INSERT INTO control (id, pause_until_utc, pause_reason, updated_time_utc)
        VALUES (1, '', '', ?)
        ON CONFLICT(id) DO UPDATE SET
          pause_until_utc='',
          pause_reason='',
          updated_time_utc=excluded.updated_time_utc
    """, (utc_now_iso(),))

    # set pet back to egg-ish state (API side only; bot still needs logic to respect it)
    conn.execute("""
        INSERT INTO pet (id, time_utc, stage, mood, health, hunger, growth, fainted_until_utc, survival_mode)
        VALUES (1, ?, 'egg', 'focused', 100.0, 50.0, 0.0, '', 'NORMAL')
        ON CONFLICT(id) DO UPDATE SET
          time_utc=excluded.time_utc,
          stage=excluded.stage,
          mood=excluded.mood,
          health=excluded.health,
          hunger=excluded.hunger,
          growth=excluded.growth,
          fainted_until_utc=excluded.fainted_until_utc,
          survival_mode=excluded.survival_mode
    """, (utc_now_iso(),))

    conn.commit()
    conn.close()

    _add_event("info", "Revive executed (pet reset to egg)", {"reason": reason})

    return jsonify({"ok": True})


def _add_event(t, message, details=None):
    conn = db()
    conn.execute("""
        INSERT INTO events (time_utc, type, message, details)
        VALUES (?, ?, ?, ?)
    """, (
        utc_now_iso(),
        t,
        message,
        json.dumps(details or {})
    ))
    conn.commit()
    conn.close()

# -----------------------------------------------------------------------------
# DELETE endpoints (wipe/reset)
# -----------------------------------------------------------------------------
@app.delete("/reset/all")
def reset_all():
    conn = db()
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM equity")
    conn.execute("DELETE FROM trades")
    conn.execute("DELETE FROM heartbeat")
    conn.execute("DELETE FROM pet")
    conn.execute("DELETE FROM prices")
    conn.execute("DELETE FROM deaths")
    conn.execute("UPDATE control SET pause_until_utc='', pause_reason='', updated_time_utc=? WHERE id=1", (utc_now_iso(),))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "reset": "all"})


@app.delete("/reset/events")
def reset_events():
    conn = db()
    conn.execute("DELETE FROM events")
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "reset": "events"})


@app.delete("/reset/trades")
def reset_trades():
    conn = db()
    conn.execute("DELETE FROM trades")
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "reset": "trades"})


@app.delete("/reset/equity")
def reset_equity():
    conn = db()
    conn.execute("DELETE FROM equity")
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "reset": "equity"})


@app.delete("/reset/deaths")
def reset_deaths():
    conn = db()
    conn.execute("DELETE FROM deaths")
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "reset": "deaths"})

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

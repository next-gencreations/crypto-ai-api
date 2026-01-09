import os
import json
import sqlite3
import math
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)

# ----------------------------
# CORS
# ----------------------------
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")
if (CORS_ORIGINS or "").strip() == "*":
    CORS(app, resources={r"/*": {"origins": "*"}})
else:
    allowed = [o.strip() for o in (CORS_ORIGINS or "").split(",") if o.strip()]
    CORS(app, resources={r"/*": {"origins": allowed}})

# ----------------------------
# Settings (bankroll)
# ----------------------------
SETTINGS_PATH = os.getenv("SETTINGS_PATH", "/var/data/settings.json")
GBPUSD_RATE = float(os.getenv("GBPUSD_RATE", "1.27"))  # simple fixed rate


def _ensure_parent_dir(path: str):
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def load_settings():
    try:
        if not os.path.exists(SETTINGS_PATH):
            return {"bankroll_gbp": 100.0}
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"bankroll_gbp": 100.0}
        if "bankroll_gbp" not in data:
            data["bankroll_gbp"] = 100.0
        return data
    except Exception:
        return {"bankroll_gbp": 100.0}


def save_settings(data: dict):
    _ensure_parent_dir(SETTINGS_PATH)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)


def get_bankroll_gbp() -> float:
    s = load_settings()
    try:
        return float(s.get("bankroll_gbp", 100.0))
    except Exception:
        return 100.0


def set_bankroll_gbp(v: float) -> float:
    v = max(0.0, float(v))
    s = load_settings()
    s["bankroll_gbp"] = v
    save_settings(s)
    return v


# ----------------------------
# Database config
# ----------------------------
DB_PATH = os.getenv("DB_PATH", "/var/data/data.db")


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_db_dir():
    parent = os.path.dirname(DB_PATH)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def get_conn():
    """
    Connection settings tuned for Render + SQLite persistence.
    WAL mode reduces locking issues for concurrent reads/writes.
    busy_timeout avoids immediate failures under load.
    """
    ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA busy_timeout=5000;")  # 5s
    except Exception:
        pass
    return conn


def _safe_json_loads(s):
    if s is None:
        return None
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s)
    except Exception:
        return None


def _to_epoch(iso_utc: str) -> int:
    try:
        s = (iso_utc or "").replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return int(datetime.now(timezone.utc).timestamp())


def _epoch_to_iso(epoch_s: int) -> str:
    try:
        return datetime.fromtimestamp(int(epoch_s), tz=timezone.utc).replace(microsecond=0).isoformat()
    except Exception:
        return utc_now_iso()


def _safe_markets_list(m):
    """
    Accept markets in any of these shapes:
      - list: ["BTCUSDT","ETHUSDT"]
      - json string: '["BTCUSDT","ETHUSDT"]'
      - single string: "BTCUSDT"
      - None/empty
    Always returns a list[str].
    """
    if m is None:
        return []
    if isinstance(m, list):
        return [str(x) for x in m if str(x).strip()]
    if isinstance(m, str):
        s = m.strip()
        if not s:
            return []
        parsed = _safe_json_loads(s)
        if isinstance(parsed, list):
            return [str(x) for x in parsed if str(x).strip()]
        return [s]
    return []


# ----------------------------
# Schema
# ----------------------------
SCHEMA_VERSION = int(os.getenv("SCHEMA_VERSION", "2"))

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS meta (
      id INTEGER PRIMARY KEY CHECK (id = 1),
      schema_version INTEGER DEFAULT 1,
      updated_time_utc TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS control (
      id INTEGER PRIMARY KEY CHECK (id = 1),
      state TEXT DEFAULT 'ACTIVE',              -- ACTIVE | CRYO | PAUSED
      pause_reason TEXT DEFAULT '',
      pause_until_utc TEXT DEFAULT '',
      cryo_reason TEXT DEFAULT '',
      cryo_until_utc TEXT DEFAULT '',
      updated_time_utc TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS heartbeat (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
      time_epoch INTEGER NOT NULL,
      equity_usd REAL DEFAULT 0,
      wins INTEGER DEFAULT 0,
      losses INTEGER DEFAULT 0,
      total_trades INTEGER DEFAULT 0,
      total_pnl_usd REAL DEFAULT 0,
      markets TEXT DEFAULT '[]',          -- JSON list
      open_positions INTEGER DEFAULT 0,
      prices_ok INTEGER DEFAULT 0,        -- 0/1
      status TEXT DEFAULT 'stopped',
      survival_mode TEXT DEFAULT 'NORMAL'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pet (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
      time_epoch INTEGER NOT NULL,
      fainted_until_utc TEXT DEFAULT '',
      growth REAL DEFAULT 0,
      health REAL DEFAULT 100,
      hunger REAL DEFAULT 0,
      mood TEXT DEFAULT 'neutral',
      stage TEXT DEFAULT 'egg',
      sex TEXT DEFAULT 'boy'              -- cosmetic: boy/girl
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS prices (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
      time_epoch INTEGER NOT NULL,
      market TEXT NOT NULL,
      price REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS equity (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
      time_epoch INTEGER NOT NULL,
      equity_usd REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trades (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
      time_epoch INTEGER NOT NULL,
      market TEXT NOT NULL,
      side TEXT NOT NULL,                -- buy/sell
      size_usd REAL DEFAULT 0,
      price REAL DEFAULT 0,
      pnl_usd REAL DEFAULT 0,
      confidence REAL DEFAULT 0,
      reason TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
      time_epoch INTEGER NOT NULL,
      type TEXT DEFAULT 'info',
      message TEXT DEFAULT '',
      details TEXT DEFAULT ''            -- JSON
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deaths (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
      time_epoch INTEGER NOT NULL,
      source TEXT DEFAULT 'bot',
      reason TEXT DEFAULT '',
      details TEXT DEFAULT ''            -- JSON
    )
    """
]

# ----------------------------
# Auto-migration (LOCKS / UPGRADES SCHEMA)
# ----------------------------
EXPECTED_COLUMNS = {
    "meta": {
        "schema_version": "INTEGER DEFAULT 1",
        "updated_time_utc": "TEXT DEFAULT ''",
    },
    "control": {
        "state": "TEXT DEFAULT 'ACTIVE'",
        "pause_reason": "TEXT DEFAULT ''",
        "pause_until_utc": "TEXT DEFAULT ''",
        "cryo_reason": "TEXT DEFAULT ''",
        "cryo_until_utc": "TEXT DEFAULT ''",
        "updated_time_utc": "TEXT DEFAULT ''",
    },
    "heartbeat": {
        "time_utc": "TEXT DEFAULT ''",
        "time_epoch": "INTEGER DEFAULT 0",
        "equity_usd": "REAL DEFAULT 0",
        "wins": "INTEGER DEFAULT 0",
        "losses": "INTEGER DEFAULT 0",
        "total_trades": "INTEGER DEFAULT 0",
        "total_pnl_usd": "REAL DEFAULT 0",
        "markets": "TEXT DEFAULT '[]'",
        "open_positions": "INTEGER DEFAULT 0",
        "prices_ok": "INTEGER DEFAULT 0",
        "status": "TEXT DEFAULT 'stopped'",
        "survival_mode": "TEXT DEFAULT 'NORMAL'",
    },
    "pet": {
        "time_utc": "TEXT DEFAULT ''",
        "time_epoch": "INTEGER DEFAULT 0",
        "fainted_until_utc": "TEXT DEFAULT ''",
        "growth": "REAL DEFAULT 0",
        "health": "REAL DEFAULT 100",
        "hunger": "REAL DEFAULT 0",
        "mood": "TEXT DEFAULT 'neutral'",
        "stage": "TEXT DEFAULT 'egg'",
        "sex": "TEXT DEFAULT 'boy'",
    },
    "prices": {
        "time_utc": "TEXT DEFAULT ''",
        "time_epoch": "INTEGER DEFAULT 0",
        "market": "TEXT DEFAULT ''",
        "price": "REAL DEFAULT 0",
    },
    "equity": {
        "time_utc": "TEXT DEFAULT ''",
        "time_epoch": "INTEGER DEFAULT 0",
        "equity_usd": "REAL DEFAULT 0",
    },
    "trades": {
        "time_utc": "TEXT DEFAULT ''",
        "time_epoch": "INTEGER DEFAULT 0",
        "market": "TEXT DEFAULT ''",
        "side": "TEXT DEFAULT 'buy'",
        "size_usd": "REAL DEFAULT 0",
        "price": "REAL DEFAULT 0",
        "pnl_usd": "REAL DEFAULT 0",
        "confidence": "REAL DEFAULT 0",
        "reason": "TEXT DEFAULT ''",
    },
    "events": {
        "time_utc": "TEXT DEFAULT ''",
        "time_epoch": "INTEGER DEFAULT 0",
        "type": "TEXT DEFAULT 'info'",
        "message": "TEXT DEFAULT ''",
        "details": "TEXT DEFAULT ''",
    },
    "deaths": {
        "time_utc": "TEXT DEFAULT ''",
        "time_epoch": "INTEGER DEFAULT 0",
        "source": "TEXT DEFAULT 'bot'",
        "reason": "TEXT DEFAULT ''",
        "details": "TEXT DEFAULT ''",
    },
}


def _table_exists(conn, name: str) -> bool:
    r = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,)
    ).fetchone()
    return r is not None


def _existing_columns(conn, table: str) -> set:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1] for r in rows}
    except Exception:
        return set()


def migrate_schema():
    conn = get_conn()
    try:
        cur = conn.cursor()

        for stmt in SCHEMA:
            cur.execute(stmt)
        conn.commit()

        for table, cols in EXPECTED_COLUMNS.items():
            if not _table_exists(conn, table):
                continue
            existing = _existing_columns(conn, table)
            for col_name, col_def in cols.items():
                if col_name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")
        conn.commit()
    finally:
        conn.close()


def init_db():
    migrate_schema()

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT id FROM meta WHERE id=1")
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO meta (id, schema_version, updated_time_utc) VALUES (1, ?, ?)",
            (SCHEMA_VERSION, utc_now_iso())
        )
    else:
        cur.execute(
            "UPDATE meta SET schema_version=?, updated_time_utc=? WHERE id=1",
            (SCHEMA_VERSION, utc_now_iso())
        )

    cur.execute("SELECT id FROM control WHERE id=1")
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO control (id, state, pause_reason, pause_until_utc, cryo_reason, cryo_until_utc, updated_time_utc) "
            "VALUES (1, 'ACTIVE', '', '', '', '', ?)",
            (utc_now_iso(),)
        )

    conn.commit()
    conn.close()


init_db()

# ----------------------------
# Helpers: fetch
# ----------------------------
ALLOWED_TABLES = {"meta", "control", "heartbeat", "pet", "prices", "equity", "trades", "events", "deaths"}


def fetch_one(table: str, order_by="id DESC"):
    if table not in ALLOWED_TABLES:
        raise ValueError("Invalid table")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM {table} ORDER BY {order_by} LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def fetch_many(table: str, limit=50, order_by="id DESC"):
    if table not in ALLOWED_TABLES:
        raise ValueError("Invalid table")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM {table} ORDER BY {order_by} LIMIT ?", (int(limit),))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def insert_row(table: str, data: dict):
    if table not in ALLOWED_TABLES:
        raise ValueError("Invalid table")
    conn = get_conn()
    cur = conn.cursor()
    cols = list(data.keys())
    vals = [data[c] for c in cols]
    placeholders = ",".join(["?"] * len(cols))
    cur.execute(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
        vals
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def add_event(ev_type: str, message: str, details=None):
    details = details or {}
    t = utc_now_iso()
    insert_row("events", {
        "time_utc": t,
        "time_epoch": _to_epoch(t),
        "type": ev_type,
        "message": message,
        "details": json.dumps(details)
    })


def get_control():
    c = fetch_one("control", order_by="id ASC")
    if not c:
        return {
            "id": 1,
            "state": "ACTIVE",
            "pause_reason": "",
            "pause_until_utc": "",
            "cryo_reason": "",
            "cryo_until_utc": "",
            "updated_time_utc": utc_now_iso()
        }
    return c


def _set_control_state(state: str, reason: str = "", seconds: int = 0):
    state = (state or "ACTIVE").upper()
    now_iso = utc_now_iso()

    conn = get_conn()
    cur = conn.cursor()

    if state == "ACTIVE":
        cur.execute(
            "UPDATE control SET state='ACTIVE', pause_reason='', pause_until_utc='', cryo_reason='', cryo_until_utc='', updated_time_utc=? WHERE id=1",
            (now_iso,)
        )
        conn.commit()
        conn.close()
        add_event("info", "State -> ACTIVE", {"reason": reason})
        return

    if state == "PAUSED":
        until = (datetime.now(timezone.utc) + timedelta(seconds=int(seconds or 0))).replace(microsecond=0).isoformat()
        cur.execute(
            "UPDATE control SET state='PAUSED', pause_reason=?, pause_until_utc=?, updated_time_utc=? WHERE id=1",
            (reason or "manual pause", until, now_iso)
        )
        conn.commit()
        conn.close()
        add_event("warning", "State -> PAUSED", {"pause_until_utc": until, "reason": reason})
        return

    if state == "CRYO":
        until = (datetime.now(timezone.utc) + timedelta(seconds=int(seconds or 0))).replace(microsecond=0).isoformat()
        cur.execute(
            "UPDATE control SET state='CRYO', cryo_reason=?, cryo_until_utc=?, updated_time_utc=? WHERE id=1",
            (reason or "cryo safety", until, now_iso)
        )
        conn.commit()
        conn.close()
        add_event("warning", "State -> CRYO", {"cryo_until_utc": until, "reason": reason})
        return

    conn.close()


def is_paused_or_cryo():
    c = get_control()
    now = datetime.now(timezone.utc)
    state = (c.get("state") or "ACTIVE").upper()

    pause_until = (c.get("pause_until_utc") or "").replace("Z", "+00:00")
    cryo_until = (c.get("cryo_until_utc") or "").replace("Z", "+00:00")

    paused = False
    cryo = False

    if state == "PAUSED" and pause_until:
        try:
            dt = datetime.fromisoformat(pause_until)
            paused = dt > now
        except Exception:
            paused = True

    if state == "CRYO" and cryo_until:
        try:
            dt = datetime.fromisoformat(cryo_until)
            cryo = dt > now
        except Exception:
            cryo = True

    if state in ("PAUSED", "CRYO") and not paused and not cryo:
        _set_control_state("ACTIVE", reason="timer complete")
        c = get_control()
        state = "ACTIVE"

    return state, c


# ----------------------------
# OHLC aggregation (candles from tick prices)
# ----------------------------
def compute_ohlc(market: str, interval_sec: int = 60, limit: int = 200):
    market = (market or "").strip()
    interval_sec = max(10, int(interval_sec))
    limit = max(10, min(1000, int(limit)))

    conn = get_conn()
    rows = conn.execute(
        """
        SELECT time_epoch, price
        FROM prices
        WHERE market = ?
        ORDER BY time_epoch DESC
        LIMIT ?
        """,
        (market, 5000)
    ).fetchall()
    conn.close()

    if not rows:
        return []

    ticks = [{"t": int(r["time_epoch"]), "p": float(r["price"])} for r in rows][::-1]

    buckets = {}
    for tick in ticks:
        b = (tick["t"] // interval_sec) * interval_sec
        if b not in buckets:
            buckets[b] = {
                "t": b,
                "time_utc": _epoch_to_iso(b),
                "o": tick["p"],
                "h": tick["p"],
                "l": tick["p"],
                "c": tick["p"]
            }
        else:
            d = buckets[b]
            d["h"] = max(d["h"], tick["p"])
            d["l"] = min(d["l"], tick["p"])
            d["c"] = tick["p"]

    out = [buckets[k] for k in sorted(buckets.keys())]
    return out[-limit:]


# ==========================================================
# ✅ SIGNAL (AI BRAIN v1) - EMA(12/26) + RSI(14) + cooldown
# ==========================================================

# optional: stop flipping every tick (default 30s)
SIGNAL_COOLDOWN_SEC = int(os.getenv("SIGNAL_COOLDOWN_SEC", "30"))

# in-memory cache (fine for single Render instance)
_LAST_SIGNAL = {
    "time_epoch": 0,
    "market": "",
    "side": "hold",
    "confidence": 0.5,
    "reason": "init",
    "features": {}
}


def _ema(values, period: int):
    if not values:
        return None
    period = max(1, int(period))
    k = 2 / (period + 1)
    ema = float(values[0])
    for v in values[1:]:
        ema = float(v) * k + ema * (1 - k)
    return ema


def _rsi(closes, period: int = 14):
    period = max(2, int(period))
    if len(closes) < period + 1:
        return None

    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = float(closes[i]) - float(closes[i - 1])
        if diff >= 0:
            gains += diff
        else:
            losses += abs(diff)

    if losses == 0:
        return 100.0
    rs = gains / max(1e-9, losses)
    return 100.0 - (100.0 / (1.0 + rs))


def _sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-float(x)))
    except Exception:
        return 0.5


def build_signal(market: str = "BTCUSDT", interval_sec: int = 60):
    market = (market or "BTCUSDT").strip().upper()
    interval_sec = max(10, int(interval_sec))

    # Cooldown: return last signal if called too soon for same market
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    if (
        _LAST_SIGNAL.get("market") == market
        and (now_epoch - int(_LAST_SIGNAL.get("time_epoch") or 0)) < SIGNAL_COOLDOWN_SEC
    ):
        return dict(_LAST_SIGNAL)

    # candles from ticks
    candles = compute_ohlc(market=market, interval_sec=interval_sec, limit=260)
    closes = [float(c.get("c")) for c in candles if c.get("c") is not None]

    if len(closes) < 80:
        out = {
            "market": market,
            "side": "hold",
            "confidence": 0.50,
            "reason": "not_enough_data",
            "features": {"closes": len(closes), "interval_sec": interval_sec},
        }
        _LAST_SIGNAL.update({"time_epoch": now_epoch, **out})
        return out

    # compute EMA on the SAME series end (don’t slice two different windows)
    ema_fast = _ema(closes, 12)
    ema_slow = _ema(closes, 26)
    rsi14 = _rsi(closes, 14)

    if ema_fast is None or ema_slow is None or rsi14 is None:
        out = {
            "market": market,
            "side": "hold",
            "confidence": 0.50,
            "reason": "indicator_nan",
            "features": {
                "ema_fast": ema_fast,
                "ema_slow": ema_slow,
                "rsi14": rsi14,
                "closes": len(closes),
                "interval_sec": interval_sec,
            },
        }
        _LAST_SIGNAL.update({"time_epoch": now_epoch, **out})
        return out

    # normalized trend
    trend = (ema_fast - ema_slow) / max(1e-9, ema_slow)

    # RSI bias (gentle)
    rsi_bias = 0.0
    if rsi14 < 33:
        rsi_bias = +0.45
    elif rsi14 > 67:
        rsi_bias = -0.45

    # score: trend scaled + rsi bias
    score = (trend * 35.0) + rsi_bias

    # confidence mapping
    conf_strength = abs(_sigmoid(score) - 0.5) * 2.0  # 0..1
    confidence = 0.50 + (conf_strength * 0.45)        # 0.50..0.95ish

    # decision thresholds
    if score > 0.22:
        side = "buy"
        reason = "ema_up_or_oversold"
    elif score < -0.22:
        side = "sell"
        reason = "ema_down_or_overbought"
    else:
        side = "hold"
        reason = "no_edge"

    out = {
        "market": market,
        "side": side,
        "confidence": float(max(0.0, min(1.0, confidence))),
        "reason": reason,
        "features": {
            "interval_sec": interval_sec,
            "ema_fast": float(ema_fast),
            "ema_slow": float(ema_slow),
            "trend": float(trend),
            "rsi14": float(rsi14),
            "score": float(score),
            "closes": len(closes),
        },
    }

    # helpful debug log when we have an actual trade suggestion
    if side != "hold" and out["confidence"] >= 0.60:
        add_event("signal", f"{market} {side.upper()} ({out['confidence']:.2f})", {"reason": reason, **out["features"]})

    _LAST_SIGNAL.update({"time_epoch": now_epoch, **out})
    return out


@app.get("/signal")
def signal():
    market = request.args.get("market", "BTCUSDT")
    interval = int(request.args.get("interval", "60"))
    out = build_signal(market=market, interval_sec=interval)
    return jsonify(out)


# ----------------------------
# Settings routes
# ----------------------------
@app.get("/settings")
def get_settings_route():
    bankroll_gbp = get_bankroll_gbp()
    bankroll_usd = bankroll_gbp * GBPUSD_RATE
    return jsonify({
        "bankroll_gbp": bankroll_gbp,
        "gbpusd_rate": GBPUSD_RATE,
        "bankroll_usd": bankroll_usd,
    })


@app.post("/settings")
def set_settings_route():
    body = request.get_json(force=True, silent=True) or {}

    if "bankroll_gbp" in body:
        bankroll_gbp = float(body.get("bankroll_gbp", 0))
    elif "bankroll_usd" in body:
        bankroll_usd = float(body.get("bankroll_usd", 0))
        bankroll_gbp = bankroll_usd / GBPUSD_RATE if GBPUSD_RATE else 0.0
    else:
        return jsonify({"ok": False, "error": "Provide bankroll_gbp (preferred) or bankroll_usd"}), 400

    bankroll_gbp = set_bankroll_gbp(bankroll_gbp)
    bankroll_usd = bankroll_gbp * GBPUSD_RATE

    add_event("info", "Settings updated", {"bankroll_gbp": bankroll_gbp, "bankroll_usd": bankroll_usd})

    return jsonify({
        "ok": True,
        "bankroll_gbp": bankroll_gbp,
        "gbpusd_rate": GBPUSD_RATE,
        "bankroll_usd": bankroll_usd,
    })


# ----------------------------
# Routes
# ----------------------------
@app.get("/health")
def health():
    return jsonify({"ok": True, "time_utc": utc_now_iso()})


@app.get("/")
def home():
    parent = os.path.dirname(DB_PATH)
    return jsonify({
        "ok": True,
        "service": "crypto-ai-api",
        "time_utc": utc_now_iso(),
        "db_parent_exists": os.path.exists(parent),
        "db_path": DB_PATH,
        "schema_version": fetch_one("meta", order_by="id ASC") or {},
        "endpoints": {
            "GET": ["/", "/health", "/schema", "/signal", "/data", "/heartbeat", "/pet", "/events", "/logs", "/equity", "/trades", "/prices", "/ohlc", "/deaths", "/control", "/settings"],
            "POST": [
                "/ingest/heartbeat", "/ingest/pet", "/ingest/event", "/ingest/equity", "/ingest/trade", "/ingest/prices", "/ingest/death",
                "/control/pause", "/control/cryo", "/control/revive",
                "/settings"
            ],
            "DELETE": ["/reset/all", "/reset/events", "/reset/trades", "/reset/equity", "/reset/prices", "/reset/deaths"]
        }
    })


@app.get("/schema")
def schema():
    conn = get_conn()
    out = {}
    try:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        for t in tables:
            name = t["name"]
            cols = conn.execute(f"PRAGMA table_info({name})").fetchall()
            out[name] = [{"name": c[1], "type": c[2]} for c in cols]
    finally:
        conn.close()
    return jsonify(out)


@app.get("/control")
def control_get():
    return jsonify(get_control())


@app.get("/data")
def data():
    state, ctrl = is_paused_or_cryo()

    hb = fetch_one("heartbeat")
    pet = fetch_one("pet")

    equity_points = fetch_many("equity", limit=200, order_by="id DESC")
    equity_points.reverse()

    recent_trades = fetch_many("trades", limit=80, order_by="id DESC")

    latest_prices = fetch_many("prices", limit=1200, order_by="id DESC")

    events = fetch_many("events", limit=250, order_by="id DESC")
    events.reverse()
    for e in events:
        e["details"] = _safe_json_loads(e.get("details"))

    deaths = fetch_many("deaths", limit=200, order_by="id DESC")
    deaths.reverse()
    for d in deaths:
        d["details"] = _safe_json_loads(d.get("details"))

    if hb:
        hb["markets"] = _safe_markets_list(_safe_json_loads(hb.get("markets")) or hb.get("markets"))
        hb["prices_ok"] = int(hb.get("prices_ok") or 0)

    latest_by_market = {}
    for p in latest_prices:
        m = p.get("market")
        if m and m not in latest_by_market:
            latest_by_market[m] = p

    bankroll_gbp = get_bankroll_gbp()
    bankroll_usd = bankroll_gbp * GBPUSD_RATE

    return jsonify({
        "control": ctrl,
        "state": state,
        "heartbeat": hb or {},
        "pet": pet or {},
        "equity": [{"equity_usd": float(p["equity_usd"]), "time_utc": p["time_utc"]} for p in equity_points],
        "trades": [
            {
                "time_utc": t.get("time_utc", ""),
                "market": t.get("market", ""),
                "side": t.get("side", ""),
                "size_usd": float(t.get("size_usd") or 0),
                "price": float(t.get("price") or 0),
                "pnl_usd": float(t.get("pnl_usd") or 0),
                "confidence": float(t.get("confidence") or 0),
                "reason": t.get("reason") or ""
            } for t in recent_trades
        ],
        "prices": latest_prices,
        "latest_prices": latest_by_market,
        "events": events,
        "deaths": deaths,
        "settings": {
            "bankroll_gbp": bankroll_gbp,
            "gbpusd_rate": GBPUSD_RATE,
            "bankroll_usd": bankroll_usd,
        },
        "stats": {
            "paused": state in ("PAUSED", "CRYO"),
            "state": state,
            "pause_until_utc": ctrl.get("pause_until_utc", ""),
            "pause_reason": ctrl.get("pause_reason", ""),
            "cryo_until_utc": ctrl.get("cryo_until_utc", ""),
            "cryo_reason": ctrl.get("cryo_reason", ""),
            "total_trades_loaded": len(recent_trades),
        }
    })


@app.get("/ohlc")
def ohlc():
    market = request.args.get("market", "BTCUSDT")
    interval = int(request.args.get("interval", "60"))
    limit = int(request.args.get("limit", "200"))
    candles = compute_ohlc(market=market, interval_sec=interval, limit=limit)
    return jsonify({"market": market, "interval_sec": interval, "candles": candles})


@app.get("/heartbeat")
def get_heartbeat():
    return jsonify(fetch_one("heartbeat") or {})


@app.get("/pet")
def get_pet():
    return jsonify(fetch_one("pet") or {})


@app.get("/events")
def get_events():
    ev = fetch_many("events", limit=250)
    for e in ev:
        e["details"] = _safe_json_loads(e.get("details"))
    return jsonify(ev)


@app.get("/logs")
def get_logs():
    limit = int(request.args.get("limit", "120"))
    limit = max(10, min(500, limit))

    ev = fetch_many("events", limit=limit, order_by="id DESC")
    lines = []
    for e in ev:
        t = (e.get("time_utc") or "").replace("T", " ").replace("+00:00", "Z")
        typ = (e.get("type") or "info").upper()
        msg = e.get("message") or ""
        lines.append(f"{t} [{typ}] {msg}")

    return jsonify(lines)


@app.get("/equity")
def get_equity():
    points = fetch_many("equity", limit=400, order_by="id DESC")
    points.reverse()
    return jsonify(points)


@app.get("/trades")
def get_trades():
    return jsonify(fetch_many("trades", limit=300))


@app.get("/prices")
def get_prices():
    return jsonify(fetch_many("prices", limit=1500))


@app.get("/deaths")
def get_deaths():
    d = fetch_many("deaths", limit=300)
    for x in d:
        x["details"] = _safe_json_loads(x.get("details"))
    return jsonify(d)


# ----------------------------
# Ingest endpoints
# ----------------------------
@app.post("/ingest/equity")
def ingest_equity():
    body = request.get_json(force=True, silent=True) or {}
    equity_usd = float(body.get("equity_usd", 0))
    time_utc = body.get("time_utc") or utc_now_iso()
    insert_row("equity", {"time_utc": time_utc, "time_epoch": _to_epoch(time_utc), "equity_usd": equity_usd})
    return jsonify({"ok": True})


@app.post("/ingest/heartbeat")
def ingest_heartbeat():
    body = request.get_json(force=True, silent=True) or {}
    time_utc = body.get("time_utc") or utc_now_iso()
    row = {
        "time_utc": time_utc,
        "time_epoch": _to_epoch(time_utc),
        "equity_usd": float(body.get("equity_usd", 0)),
        "wins": int(body.get("wins", 0)),
        "losses": int(body.get("losses", 0)),
        "total_trades": int(body.get("total_trades", 0)),
        "total_pnl_usd": float(body.get("total_pnl_usd", 0)),
        "markets": json.dumps(body.get("markets", [])),
        "open_positions": int(body.get("open_positions", 0)),
        "prices_ok": int(bool(body.get("prices_ok", False))),
        "status": body.get("status", "running"),
        "survival_mode": body.get("survival_mode", "NORMAL"),
    }
    insert_row("heartbeat", row)
    return jsonify({"ok": True})


@app.post("/ingest/pet")
def ingest_pet():
    body = request.get_json(force=True, silent=True) or {}
    time_utc = body.get("time_utc") or utc_now_iso()
    row = {
        "time_utc": time_utc,
        "time_epoch": _to_epoch(time_utc),
        "fainted_until_utc": body.get("fainted_until_utc", "") or "",
        "growth": float(body.get("growth", 0)),
        "health": float(body.get("health", 100)),
        "hunger": float(body.get("hunger", 0)),
        "mood": body.get("mood", "neutral"),
        "stage": body.get("stage", "egg"),
        "sex": body.get("sex", "boy"),
    }
    insert_row("pet", row)
    return jsonify({"ok": True})


@app.post("/ingest/trade")
def ingest_trade():
    body = request.get_json(force=True, silent=True) or {}
    time_utc = body.get("time_utc") or utc_now_iso()
    row = {
        "time_utc": time_utc,
        "time_epoch": _to_epoch(time_utc),
        "market": body.get("market", "BTCUSDT"),
        "side": body.get("side", "buy"),
        "size_usd": float(body.get("size_usd", 0)),
        "price": float(body.get("price", 0)),
        "pnl_usd": float(body.get("pnl_usd", 0)),
        "confidence": float(body.get("confidence", 0)),
        "reason": body.get("reason", "") or "",
    }
    insert_row("trades", row)
    return jsonify({"ok": True})


@app.post("/ingest/prices")
def ingest_prices():
    body = request.get_json(force=True, silent=True) or {}
    time_utc = body.get("time_utc") or utc_now_iso()
    time_epoch = _to_epoch(time_utc)

    prices = body.get("prices", None)
    if prices is None:
        prices = body

    if not isinstance(prices, dict):
        return jsonify({"ok": False, "error": "prices must be a dict"}), 400

    count = 0
    for market, price in prices.items():
        try:
            insert_row("prices", {"time_utc": time_utc, "time_epoch": time_epoch, "market": str(market), "price": float(price)})
            count += 1
        except Exception:
            pass

    return jsonify({"ok": True, "count": count})


@app.post("/ingest/event")
def ingest_event():
    body = request.get_json(force=True, silent=True) or {}
    t = body.get("time_utc") or utc_now_iso()
    insert_row("events", {
        "time_utc": t,
        "time_epoch": _to_epoch(t),
        "type": body.get("type", "info"),
        "message": body.get("message", "") or "",
        "details": json.dumps(body.get("details", {}))
    })
    return jsonify({"ok": True})


@app.post("/ingest/death")
def ingest_death():
    body = request.get_json(force=True, silent=True) or {}
    t = body.get("time_utc") or utc_now_iso()
    insert_row("deaths", {
        "time_utc": t,
        "time_epoch": _to_epoch(t),
        "source": body.get("source", "bot"),
        "reason": body.get("reason", "") or "",
        "details": json.dumps(body.get("details", {}))
    })
    add_event("warning", "Death/Cryo record added", {"reason": body.get("reason", ""), "source": body.get("source", "bot")})
    return jsonify({"ok": True})


# ----------------------------
# Control endpoints
# ----------------------------
@app.post("/control/pause")
def control_pause():
    body = request.get_json(force=True, silent=True) or {}
    seconds = int(body.get("seconds", 600))
    reason = body.get("reason", "manual pause")
    _set_control_state("PAUSED", reason=reason, seconds=seconds)
    c = get_control()
    return jsonify({"ok": True, "state": "PAUSED", "pause_until_utc": c.get("pause_until_utc", ""), "reason": reason})


@app.post("/control/cryo")
def control_cryo():
    body = request.get_json(force=True, silent=True) or {}
    seconds = int(body.get("seconds", 600))
    reason = body.get("reason", "cryo safety")
    _set_control_state("CRYO", reason=reason, seconds=seconds)
    c = get_control()
    return jsonify({"ok": True, "state": "CRYO", "cryo_until_utc": c.get("cryo_until_utc", ""), "reason": reason})


@app.post("/control/revive")
def control_revive():
    body = request.get_json(force=True, silent=True) or {}
    reason = body.get("reason", "revive")
    _set_control_state("ACTIVE", reason=reason)
    add_event("info", "Revive executed", {"reason": reason})
    return jsonify({"ok": True, "state": "ACTIVE"})


# ----------------------------
# Reset endpoints
# ----------------------------
def wipe_table(name):
    if name not in ALLOWED_TABLES:
        raise ValueError("Invalid table")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {name}")
    conn.commit()
    conn.close()


@app.delete("/reset/all")
def reset_all():
    for t in ["heartbeat", "pet", "prices", "equity", "trades", "events", "deaths"]:
        wipe_table(t)
    _set_control_state("ACTIVE", reason="reset/all")
    return jsonify({"ok": True})


@app.delete("/reset/events")
def reset_events():
    wipe_table("events")
    return jsonify({"ok": True})


@app.delete("/reset/trades")
def reset_trades():
    wipe_table("trades")
    return jsonify({"ok": True})


@app.delete("/reset/equity")
def reset_equity():
    wipe_table("equity")
    return jsonify({"ok": True})


@app.delete("/reset/prices")
def reset_prices():
    wipe_table("prices")
    return jsonify({"ok": True})


@app.delete("/reset/deaths")
def reset_deaths():
    wipe_table("deaths")
    return jsonify({"ok": True})


# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

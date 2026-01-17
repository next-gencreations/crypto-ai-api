import os
import json
import sqlite3
import math
import time
import base64
import secrets
import hashlib
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

from flask import Flask, request, jsonify
from flask_cors import CORS
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ==========================================================
# App
# ==========================================================
app = Flask(__name__)

# ==========================================================
# CORS
# ==========================================================
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")
if (CORS_ORIGINS or "").strip() == "*":
    CORS(app, resources={r"/*": {"origins": "*"}})
else:
    allowed = [o.strip() for o in (CORS_ORIGINS or "").split(",") if o.strip()]
    CORS(app, resources={r"/*": {"origins": allowed}})

# ==========================================================
# Settings (bankroll + brain controls)
# ==========================================================
SETTINGS_PATH = os.getenv("SETTINGS_PATH", "/var/data/settings.json")
GBPUSD_RATE = float(os.getenv("GBPUSD_RATE", "1.27"))

DEFAULT_SETTINGS = {
    "bankroll_gbp": 100.0,
    "risk_per_trade_pct": 1.0,
    "max_open_positions": 1,
    "min_trade_interval_sec": 20,
    "atr_period": 14,
    "atr_stop_mult": 1.35,
    "min_notional_usd": 20.0,
    "max_notional_usd": 900.0,
    "best_max_markets": 20,
    "best_min_confidence": 0.45,
    "trade_mode": "PAPER",  # hard safety
}

def _ensure_parent_dir(path: str):
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

def load_settings():
    try:
        if not os.path.exists(SETTINGS_PATH):
            return dict(DEFAULT_SETTINGS)
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(DEFAULT_SETTINGS)
        out = dict(DEFAULT_SETTINGS)
        out.update(data or {})
        if "bankroll_gbp" not in out:
            out["bankroll_gbp"] = DEFAULT_SETTINGS["bankroll_gbp"]
        if "trade_mode" not in out:
            out["trade_mode"] = "PAPER"
        return out
    except Exception:
        return dict(DEFAULT_SETTINGS)

def save_settings(data: dict):
    _ensure_parent_dir(SETTINGS_PATH)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)

def get_settings_public():
    s = load_settings()
    bankroll_gbp = float(s.get("bankroll_gbp", DEFAULT_SETTINGS["bankroll_gbp"]))
    bankroll_usd = bankroll_gbp * GBPUSD_RATE
    return {
        "bankroll_gbp": bankroll_gbp,
        "gbpusd_rate": GBPUSD_RATE,
        "bankroll_usd": bankroll_usd,
        "trade_mode": (s.get("trade_mode") or "PAPER").upper(),
        "risk_per_trade_pct": float(s.get("risk_per_trade_pct", DEFAULT_SETTINGS["risk_per_trade_pct"])),
        "max_open_positions": int(s.get("max_open_positions", DEFAULT_SETTINGS["max_open_positions"])),
        "min_trade_interval_sec": int(s.get("min_trade_interval_sec", DEFAULT_SETTINGS["min_trade_interval_sec"])),
        "atr_period": int(s.get("atr_period", DEFAULT_SETTINGS["atr_period"])),
        "atr_stop_mult": float(s.get("atr_stop_mult", DEFAULT_SETTINGS["atr_stop_mult"])),
        "min_notional_usd": float(s.get("min_notional_usd", DEFAULT_SETTINGS["min_notional_usd"])),
        "max_notional_usd": float(s.get("max_notional_usd", DEFAULT_SETTINGS["max_notional_usd"])),
        "best_max_markets": int(s.get("best_max_markets", DEFAULT_SETTINGS["best_max_markets"])),
        "best_min_confidence": float(s.get("best_min_confidence", DEFAULT_SETTINGS["best_min_confidence"])),
    }

# ==========================================================
# Database
# ==========================================================
DB_PATH = os.getenv("DB_PATH", "/var/data/data.db")

def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def ensure_db_dir():
    parent = os.path.dirname(DB_PATH)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

def get_conn():
    ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA busy_timeout=5000;")
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
    if m is None:
        return []
    if isinstance(m, list):
        return [str(x).strip().upper() for x in m if str(x).strip()]
    if isinstance(m, str):
        s = m.strip()
        if not s:
            return []
        parsed = _safe_json_loads(s)
        if isinstance(parsed, list):
            return [str(x).strip().upper() for x in parsed if str(x).strip()]
        return [s.upper()]
    return []

# ----------------------------
# Schema (core)
# ----------------------------
SCHEMA_VERSION = int(os.getenv("SCHEMA_VERSION", "3"))

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
      state TEXT DEFAULT 'ACTIVE',
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
      markets TEXT DEFAULT '[]',
      open_positions INTEGER DEFAULT 0,
      prices_ok INTEGER DEFAULT 0,
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
      sex TEXT DEFAULT 'boy'
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
      side TEXT NOT NULL,
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
      details TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deaths (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time_utc TEXT NOT NULL,
      time_epoch INTEGER NOT NULL,
      source TEXT DEFAULT 'bot',
      reason TEXT DEFAULT '',
      details TEXT DEFAULT ''
    )
    """,
]

def migrate_schema():
    conn = get_conn()
    try:
        cur = conn.cursor()
        for stmt in SCHEMA:
            cur.execute(stmt)
        conn.commit()
    finally:
        conn.close()

def init_db():
    migrate_schema()
    conn = get_conn()
    try:
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
    finally:
        conn.close()

init_db()

# ----------------------------
# Helpers: fetch/insert
# ----------------------------
ALLOWED_TABLES = {"meta", "control", "heartbeat", "pet", "prices", "equity", "trades", "events", "deaths"}

def fetch_one(table: str, order_by="id DESC"):
    if table not in ALLOWED_TABLES:
        raise ValueError("Invalid table")
    conn = get_conn()
    try:
        row = conn.execute(f"SELECT * FROM {table} ORDER BY {order_by} LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def fetch_many(table: str, limit=50, order_by="id DESC"):
    if table not in ALLOWED_TABLES:
        raise ValueError("Invalid table")
    conn = get_conn()
    try:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order_by} LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def insert_row(table: str, data: dict):
    if table not in ALLOWED_TABLES:
        raise ValueError("Invalid table")
    conn = get_conn()
    try:
        cols = list(data.keys())
        vals = [data[c] for c in cols]
        placeholders = ",".join(["?"] * len(cols))
        cur = conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})", vals)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()

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

# ==========================================================
# Control helpers
# ==========================================================
def get_control():
    c = fetch_one("control", order_by="id ASC")
    if not c:
        return {
            "id": 1, "state": "ACTIVE",
            "pause_reason": "", "pause_until_utc": "",
            "cryo_reason": "", "cryo_until_utc": "",
            "updated_time_utc": utc_now_iso()
        }
    return c

def _set_control_state(state: str, reason: str = "", seconds: int = 0):
    state = (state or "ACTIVE").upper()
    now_iso = utc_now_iso()

    conn = get_conn()
    try:
        if state == "ACTIVE":
            conn.execute(
                "UPDATE control SET state='ACTIVE', pause_reason='', pause_until_utc='', cryo_reason='', cryo_until_utc='', updated_time_utc=? WHERE id=1",
                (now_iso,)
            )
            conn.commit()
            add_event("info", "State -> ACTIVE", {"reason": reason})
            return

        if state == "PAUSED":
            until = (datetime.now(timezone.utc) + timedelta(seconds=int(seconds or 0))).replace(microsecond=0).isoformat()
            conn.execute(
                "UPDATE control SET state='PAUSED', pause_reason=?, pause_until_utc=?, updated_time_utc=? WHERE id=1",
                (reason or "manual pause", until, now_iso)
            )
            conn.commit()
            add_event("warning", "State -> PAUSED", {"pause_until_utc": until, "reason": reason})
            return

        if state == "CRYO":
            until = (datetime.now(timezone.utc) + timedelta(seconds=int(seconds or 0))).replace(microsecond=0).isoformat()
            conn.execute(
                "UPDATE control SET state='CRYO', cryo_reason=?, cryo_until_utc=?, updated_time_utc=? WHERE id=1",
                (reason or "cryo safety", until, now_iso)
            )
            conn.commit()
            add_event("warning", "State -> CRYO", {"cryo_until_utc": until, "reason": reason})
            return
    finally:
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

# ==========================================================
# OHLC aggregation
# ==========================================================
def compute_ohlc(market: str, interval_sec: int = 60, limit: int = 200):
    market = (market or "").strip().upper()
    interval_sec = max(10, int(interval_sec))
    limit = max(10, min(1000, int(limit)))

    conn = get_conn()
    try:
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
    finally:
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
                "c": tick["p"],
            }
        else:
            d = buckets[b]
            d["h"] = max(d["h"], tick["p"])
            d["l"] = min(d["l"], tick["p"])
            d["c"] = tick["p"]

    out = [buckets[k] for k in sorted(buckets.keys())]
    return out[-limit:]

# ==========================================================
# Brain v1: EMA + RSI + ATR sizing
# ==========================================================
SIGNAL_COOLDOWN_SEC = int(os.getenv("SIGNAL_COOLDOWN_SEC", "30"))
_LAST_SIGNAL = {"time_epoch": 0, "market": "", "side": "hold", "confidence": 0.5, "reason": "init", "features": {}}
_LAST_DECISION_BY_MARKET: Dict[str, Dict[str, Any]] = {}

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

def _atr(candles, period: int = 14):
    period = max(2, int(period))
    if not candles or len(candles) < period + 2:
        return None

    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i]["h"])
        l = float(candles[i]["l"])
        prev_c = float(candles[i - 1]["c"])
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)

    if len(trs) < period:
        return None

    window = trs[-period:]
    return sum(window) / max(1, len(window))

def _sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-float(x)))
    except Exception:
        return 0.5

def build_signal(market: str = "BTCUSDT", interval_sec: int = 60):
    market = (market or "BTCUSDT").strip().upper()
    interval_sec = max(10, int(interval_sec))

    now_epoch = int(datetime.now(timezone.utc).timestamp())
    if (
        _LAST_SIGNAL.get("market") == market
        and (now_epoch - int(_LAST_SIGNAL.get("time_epoch") or 0)) < SIGNAL_COOLDOWN_SEC
    ):
        return dict(_LAST_SIGNAL)

    candles = compute_ohlc(market=market, interval_sec=interval_sec, limit=260)
    closes = [float(c.get("c")) for c in candles if c.get("c") is not None]

    if len(closes) < 80:
        out = {
            "market": market, "side": "hold", "confidence": 0.50, "reason": "not_enough_data",
            "features": {"closes": len(closes), "interval_sec": interval_sec}
        }
        _LAST_SIGNAL.update({"time_epoch": now_epoch, **out})
        return out

    ema_fast = _ema(closes, 12)
    ema_slow = _ema(closes, 26)
    rsi14 = _rsi(closes, 14)

    if ema_fast is None or ema_slow is None or rsi14 is None:
        out = {
            "market": market, "side": "hold", "confidence": 0.50, "reason": "indicator_nan",
            "features": {"ema_fast": ema_fast, "ema_slow": ema_slow, "rsi14": rsi14}
        }
        _LAST_SIGNAL.update({"time_epoch": now_epoch, **out})
        return out

    trend = (ema_fast - ema_slow) / max(1e-9, ema_slow)

    rsi_bias = 0.0
    if rsi14 < 33:
        rsi_bias = +0.45
    elif rsi14 > 67:
        rsi_bias = -0.45

    score = (trend * 35.0) + rsi_bias
    conf_strength = abs(_sigmoid(score) - 0.5) * 2.0
    confidence = 0.50 + (conf_strength * 0.45)

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

    _LAST_SIGNAL.update({"time_epoch": now_epoch, **out})
    return out

def compute_position_size_usd(entry_price: float, stop_distance: float, settings_public: dict):
    bankroll_usd = float(settings_public.get("bankroll_usd") or 0.0)
    risk_pct = float(settings_public.get("risk_per_trade_pct") or DEFAULT_SETTINGS["risk_per_trade_pct"])
    min_notional = float(settings_public.get("min_notional_usd") or DEFAULT_SETTINGS["min_notional_usd"])
    max_notional = float(settings_public.get("max_notional_usd") or DEFAULT_SETTINGS["max_notional_usd"])

    if bankroll_usd <= 0 or entry_price <= 0 or stop_distance <= 0:
        return 0.0, {"why": "invalid_inputs"}

    risk_usd = bankroll_usd * (risk_pct / 100.0)
    stop_distance = max(stop_distance, entry_price * 0.001)  # min 0.1% stop
    notional = risk_usd * (entry_price / stop_distance)

    notional = max(0.0, min(max_notional, notional))
    if notional < min_notional:
        return 0.0, {"why": "below_min_notional", "notional": notional, "min_notional": min_notional}

    return float(notional), {
        "bankroll_usd": bankroll_usd,
        "risk_pct": risk_pct,
        "risk_usd": risk_usd,
        "entry": entry_price,
        "stop_distance": stop_distance,
        "notional": notional,
        "min_notional": min_notional,
        "max_notional": max_notional,
    }

def build_decision(market: str = "BTCUSDT", interval_sec: int = 60):
    market = (market or "BTCUSDT").strip().upper()
    interval_sec = max(10, int(interval_sec))

    state, _ = is_paused_or_cryo()
    if state in ("PAUSED", "CRYO"):
        return {
            "market": market,
            "action": "HOLD",
            "confidence": 0.0,
            "reason": f"state_{state.lower()}",
            "size_usd": 0.0,
            "stop_distance": 0.0,
            "features": {"interval_sec": interval_sec, "state": state},
        }

    settings_public = get_settings_public()
    now_epoch = int(datetime.now(timezone.utc).timestamp())

    last = _LAST_DECISION_BY_MARKET.get(market) or {}
    min_gap = int(settings_public.get("min_trade_interval_sec") or DEFAULT_SETTINGS["min_trade_interval_sec"])
    if last.get("time_epoch") and (now_epoch - int(last.get("time_epoch"))) < min_gap:
        return {
            "market": market,
            "action": "HOLD",
            "confidence": 0.0,
            "reason": "cooldown",
            "size_usd": 0.0,
            "stop_distance": 0.0,
            "features": {
                "interval_sec": interval_sec,
                "cooldown_remaining_sec": max(0, min_gap - (now_epoch - int(last.get("time_epoch")))),
            },
        }

    sig = build_signal(market=market, interval_sec=interval_sec)
    candles = compute_ohlc(market=market, interval_sec=interval_sec, limit=260)
    if not candles or len(candles) < 25:
        return {
            "market": market,
            "action": "HOLD",
            "confidence": 0.0,
            "reason": "no_candles",
            "size_usd": 0.0,
            "stop_distance": 0.0,
            "features": {"interval_sec": interval_sec},
        }

    entry = float(candles[-1]["c"])
    atr_period = int(settings_public.get("atr_period") or DEFAULT_SETTINGS["atr_period"])
    atr = _atr(candles, period=atr_period)
    if atr is None or not (atr > 0):
        return {
            "market": market,
            "action": "HOLD",
            "confidence": 0.0,
            "reason": "atr_nan",
            "size_usd": 0.0,
            "stop_distance": 0.0,
            "features": {"interval_sec": interval_sec, "entry": entry},
        }

    stop_mult = float(settings_public.get("atr_stop_mult") or DEFAULT_SETTINGS["atr_stop_mult"])
    stop_distance = float(atr * stop_mult)

    side = (sig.get("side") or "hold").lower()
    conf = float(sig.get("confidence") or 0.0)
    reason = str(sig.get("reason") or "no_reason")

    min_conf = float(settings_public.get("best_min_confidence", DEFAULT_SETTINGS["best_min_confidence"]))
    if side == "hold" or conf < min_conf:
        _LAST_DECISION_BY_MARKET[market] = {"time_epoch": now_epoch, "action": "HOLD"}
        return {
            "market": market,
            "action": "HOLD",
            "confidence": conf,
            "reason": "no_trade_edge",
            "size_usd": 0.0,
            "stop_distance": stop_distance,
            "features": {"interval_sec": interval_sec, "entry": entry, "atr": float(atr), "sig": sig},
        }

    size_usd, sizing_meta = compute_position_size_usd(entry, stop_distance, settings_public)
    if size_usd <= 0:
        _LAST_DECISION_BY_MARKET[market] = {"time_epoch": now_epoch, "action": "HOLD"}
        return {
            "market": market,
            "action": "HOLD",
            "confidence": conf,
            "reason": f"sizing_blocked:{sizing_meta.get('why','unknown')}",
            "size_usd": 0.0,
            "stop_distance": stop_distance,
            "features": {"interval_sec": interval_sec, "entry": entry, "atr": float(atr), "sig": sig, "sizing": sizing_meta},
        }

    action = "BUY" if side == "buy" else "SELL"
    out = {
        "market": market,
        "action": action,
        "confidence": conf,
        "reason": reason,
        "size_usd": float(size_usd),
        "stop_distance": float(stop_distance),
        "features": {"interval_sec": interval_sec, "entry": entry, "atr": float(atr), "sig": sig, "sizing": sizing_meta},
    }

    add_event("decision", f"{market} {action} ${size_usd:.0f} ({conf:.2f})", {"reason": reason, "entry": entry, "atr": float(atr)})
    _LAST_DECISION_BY_MARKET[market] = {"time_epoch": now_epoch, "action": action}
    return out

def _list_candidate_markets(max_markets: int = 8):
    max_markets = max(1, min(50, int(max_markets)))

    hb = fetch_one("heartbeat")
    if hb:
        mk = _safe_markets_list(_safe_json_loads(hb.get("markets")) or hb.get("markets"))
        mk = [m for m in mk if m]
        if mk:
            return mk[:max_markets]

    conn = get_conn()
    try:
        rows = conn.execute("SELECT market FROM prices ORDER BY time_epoch DESC LIMIT 2000").fetchall()
    finally:
        conn.close()

    seen = set()
    out = []
    for r in rows:
        m = (r["market"] or "").strip().upper()
        if not m or m in seen:
            continue
        seen.add(m)
        out.append(m)
        if len(out) >= max_markets:
            break
    return out

def _best_score(decision_obj: dict) -> float:
    action = (decision_obj.get("action") or "HOLD").upper()
    conf = float(decision_obj.get("confidence") or 0.0)
    size = float(decision_obj.get("size_usd") or 0.0)
    if action == "HOLD":
        return 0.0
    return (conf * 100.0) + min(25.0, size / 50.0)

def build_best_decision(interval_sec: int = 60):
    settings = get_settings_public()
    max_mk = int(settings.get("best_max_markets") or DEFAULT_SETTINGS["best_max_markets"])
    min_conf = float(settings.get("best_min_confidence") or DEFAULT_SETTINGS["best_min_confidence"])

    markets = _list_candidate_markets(max_markets=max_mk)
    candidates = []
    for m in markets:
        d = build_decision(market=m, interval_sec=interval_sec)
        action = (d.get("action") or "HOLD").upper()
        conf = float(d.get("confidence") or 0.0)
        eligible = (action != "HOLD") and (conf >= min_conf)
        candidates.append({
            "market": m,
            "action": action,
            "confidence": conf,
            "reason": d.get("reason") or "",
            "size_usd": float(d.get("size_usd") or 0.0),
            "stop_distance": float(d.get("stop_distance") or 0.0),
            "eligible": bool(eligible),
            "score": float(_best_score(d)) if eligible else 0.0,
            "features": d.get("features") or {},
        })

    best = None
    for c in candidates:
        if not c.get("eligible"):
            continue
        if best is None or float(c.get("score") or 0.0) > float(best.get("score") or 0.0):
            best = c

    return {
        "ok": True,
        "interval_sec": int(interval_sec),
        "markets_checked": markets,
        "best": best or {
            "market": markets[0] if markets else "BTCUSDT",
            "action": "HOLD",
            "confidence": 0.0,
            "reason": "no_eligible_candidates",
            "size_usd": 0.0,
            "stop_distance": 0.0,
            "eligible": False,
            "score": 0.0,
            "features": {},
        },
        "candidates": candidates,
    }

# ==========================================================
# Paper Trading Engine
# ==========================================================
@dataclass
class PaperPosition:
    market: str
    side: str            # "LONG" or "SHORT"
    qty: float
    entry: float
    stop: float
    opened_ts: int

@dataclass
class PaperState:
    cash_usd: float = 1000.0
    equity_usd: float = 1000.0
    peak_equity_usd: float = 1000.0
    drawdown_pct: float = 0.0
    position: Optional[PaperPosition] = None

@dataclass
class PaperConfig:
    enabled: bool = True
    fee_bps: float = 4.0
    slippage_bps: float = 3.0
    max_drawdown_pct: float = 12.0
    rr_takeprofit: float = 1.5
    allow_shorts: bool = True
    one_position_only: bool = True

PAPER_CFG = PaperConfig()
PAPER = PaperState()
PAPER_TRADES: List[Dict[str, Any]] = []

def _bps(x: float) -> float:
    return float(x) / 10000.0

def _paper_fee(notional: float) -> float:
    return abs(float(notional)) * _bps(PAPER_CFG.fee_bps)

def _paper_apply_slippage(price: float, side: str, is_entry: bool) -> float:
    slip = _bps(PAPER_CFG.slippage_bps)
    side = (side or "").upper()
    if side == "LONG":
        return price * (1.0 + slip) if is_entry else price * (1.0 - slip)
    else:
        return price * (1.0 - slip) if is_entry else price * (1.0 + slip)

def _paper_mark_price(market: str) -> float:
    candles = compute_ohlc(market=market, interval_sec=60, limit=2)
    return float(candles[-1]["c"]) if candles else 0.0

def _paper_update_equity():
    PAPER.equity_usd = PAPER.cash_usd
    if PAPER.position:
        mark = _paper_mark_price(PAPER.position.market)
        if mark > 0:
            if PAPER.position.side == "LONG":
                pnl = (mark - PAPER.position.entry) * PAPER.position.qty
            else:
                pnl = (PAPER.position.entry - mark) * PAPER.position.qty
            PAPER.equity_usd += float(pnl)

    PAPER.peak_equity_usd = max(PAPER.peak_equity_usd, PAPER.equity_usd)
    if PAPER.peak_equity_usd > 0:
        PAPER.drawdown_pct = max(
            0.0,
            (PAPER.peak_equity_usd - PAPER.equity_usd) / PAPER.peak_equity_usd * 100.0
        )

def _paper_block_new_entries() -> Optional[str]:
    _paper_update_equity()
    if PAPER_CFG.max_drawdown_pct and PAPER.drawdown_pct >= PAPER_CFG.max_drawdown_pct:
        return "max_drawdown"
    return None

def _paper_tp_price(pos: PaperPosition) -> Optional[float]:
    rr = float(PAPER_CFG.rr_takeprofit or 0.0)
    if rr <= 0:
        return None
    r = abs(float(pos.entry) - float(pos.stop))
    if r <= 0:
        return None
    if pos.side == "LONG":
        return float(pos.entry + rr * r)
    return float(pos.entry - rr * r)

def _paper_close_position(exit_price: float, reason: str) -> Dict[str, Any]:
    pos = PAPER.position
    if not pos:
        return {"ok": False, "why": "no_position"}

    exit_exec = _paper_apply_slippage(float(exit_price), pos.side, is_entry=False)

    if pos.side == "LONG":
        pnl = (exit_exec - pos.entry) * pos.qty
    else:
        pnl = (pos.entry - exit_exec) * pos.qty

    notional_entry = pos.entry * pos.qty
    PAPER.cash_usd += notional_entry + pnl

    notional_exit = exit_exec * pos.qty
    exit_fee = _paper_fee(notional_exit)
    PAPER.cash_usd -= exit_fee

    PAPER_TRADES.append({
        "ts": int(time.time()),
        "type": "CLOSE",
        "market": pos.market,
        "side": pos.side,
        "qty": float(pos.qty),
        "entry": float(pos.entry),
        "exit": float(exit_exec),
        "pnl": float(pnl),
        "reason": reason,
        "stop": float(pos.stop),
        "tp": _paper_tp_price(pos),
        "fee": float(exit_fee),
    })

    PAPER.position = None
    _paper_update_equity()
    return {"ok": True, "closed_at": float(exit_exec), "pnl": float(pnl), "reason": reason}

def _paper_check_exit() -> Optional[Dict[str, Any]]:
    if not PAPER.position:
        return None

    mark = _paper_mark_price(PAPER.position.market)
    if mark <= 0:
        return None

    pos = PAPER.position

    if pos.side == "LONG" and mark <= pos.stop:
        return _paper_close_position(mark, "STOP_HIT")
    if pos.side == "SHORT" and mark >= pos.stop:
        return _paper_close_position(mark, "STOP_HIT")

    tp = _paper_tp_price(pos)
    if tp is not None:
        if pos.side == "LONG" and mark >= tp:
            return _paper_close_position(mark, "TAKE_PROFIT")
        if pos.side == "SHORT" and mark <= tp:
            return _paper_close_position(mark, "TAKE_PROFIT")

    return None

def _paper_open_from_decision(decision: Dict[str, Any]) -> Dict[str, Any]:
    if not PAPER_CFG.enabled:
        return {"ok": False, "why": "paper_disabled"}

    market = (decision.get("market") or "").strip().upper()
    action = (decision.get("action") or "HOLD").upper()
    size_usd = float(decision.get("size_usd") or 0.0)
    stop_distance = float(decision.get("stop_distance") or 0.0)

    entry = float((decision.get("features") or {}).get("entry") or 0.0)
    if entry <= 0:
        entry = _paper_mark_price(market)

    if not market or action not in ("BUY", "SELL") or entry <= 0 or size_usd <= 0 or stop_distance <= 0:
        return {"ok": False, "why": "bad_decision_inputs"}

    if action == "SELL" and not PAPER_CFG.allow_shorts:
        return {"ok": False, "why": "shorts_disabled"}

    if PAPER_CFG.one_position_only and PAPER.position is not None:
        return {"ok": False, "why": "position_already_open"}

    block = _paper_block_new_entries()
    if block:
        return {"ok": False, "why": block}

    side = "LONG" if action == "BUY" else "SHORT"
    entry_exec = _paper_apply_slippage(entry, side, is_entry=True)
    qty = size_usd / entry_exec
    stop = (entry_exec - stop_distance) if side == "LONG" else (entry_exec + stop_distance)

    PAPER.position = PaperPosition(
        market=market,
        side=side,
        qty=float(qty),
        entry=float(entry_exec),
        stop=float(stop),
        opened_ts=int(time.time()),
    )

    entry_fee = _paper_fee(size_usd)
    PAPER.cash_usd -= (size_usd + entry_fee)

    PAPER_TRADES.append({
        "ts": PAPER.position.opened_ts,
        "type": "OPEN",
        "market": market,
        "side": side,
        "qty": float(qty),
        "entry": float(entry_exec),
        "stop": float(stop),
        "fee": float(entry_fee),
        "meta": {
            "confidence": decision.get("confidence"),
            "reason": decision.get("reason"),
            "stop_distance": stop_distance,
            "size_usd": size_usd,
        }
    })

    _paper_update_equity()
    return {"ok": True, "opened": asdict(PAPER.position)}

# ===================== END OF PART 1 ======================
# ===== PART 2 START =====

VAULT_SESSION_TTL_SEC = int(os.getenv("VAULT_SESSION_TTL_SEC", "300"))

def _vault_key_bytes() -> Optional[bytes]:
    b64 = (os.getenv("VAULT_MASTER_KEY") or "").strip()
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
        return raw if len(raw) == 32 else None
    except Exception:
        return None

def vault_enabled() -> bool:
    return _vault_key_bytes() is not None

def _vault_now() -> int:
    return int(time.time())

def _pbkdf2_hash_pin(pin: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, 200_000, dklen=32)

def _format_pin_hash(pin: str) -> str:
    salt = os.urandom(16)
    h = _pbkdf2_hash_pin(pin, salt)
    return f"{base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"

def _verify_pin(pin: str, stored: str) -> bool:
    try:
        salt_b64, h_b64 = (stored or "").split("$", 1)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(h_b64)
        got = _pbkdf2_hash_pin(pin, salt)
        return secrets.compare_digest(got, expected)
    except Exception:
        return False

def init_vault_tables():
    conn = get_conn()
    try:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS vault_keys(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exchange TEXT,
            enc_key TEXT,
            nonce TEXT,
            created TEXT
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS vault_state(
            id INTEGER PRIMARY KEY CHECK (id=1),
            pin_hash TEXT,
            unlocked_until INTEGER
        )
        """)
        conn.execute("INSERT OR IGNORE INTO vault_state(id,pin_hash,unlocked_until) VALUES(1,NULL,0)")
        conn.commit()
    finally:
        conn.close()

init_vault_tables()

def vault_unlocked() -> bool:
    conn = get_conn()
    try:
        row = conn.execute("SELECT unlocked_until FROM vault_state WHERE id=1").fetchone()
        return int(row["unlocked_until"] or 0) > _vault_now()
    finally:
        conn.close()

def _vault_encrypt_value(raw: str):
    key = _vault_key_bytes()
    if not key:
        raise ValueError("vault_not_configured")
    aes = AESGCM(key)
    nonce = os.urandom(12)
    enc = aes.encrypt(nonce, raw.encode("utf-8"), None)
    return base64.b64encode(enc).decode(), base64.b64encode(nonce).decode()

def _vault_decrypt_value(enc: str, nonce: str):
    key = _vault_key_bytes()
    if not key:
        raise ValueError("vault_not_configured")
    aes = AESGCM(key)
    return aes.decrypt(base64.b64decode(nonce), base64.b64decode(enc), None).decode("utf-8")

def _require_unlocked():
    if not vault_enabled():
        return jsonify({"ok": False, "error": "vault_not_configured"}), 400
    if not vault_unlocked():
        return jsonify({"ok": False, "error": "vault_locked"}), 401
    return None

def _mask(s: str) -> str:
    s = str(s or "")
    if len(s) <= 4:
        return "*" * len(s)
    return ("*" * (len(s) - 4)) + s[-4:]

@app.get("/vault/status")
def vault_status():
    conn = get_conn()
    try:
        r = conn.execute("SELECT pin_hash, unlocked_until FROM vault_state WHERE id=1").fetchone()
        return jsonify({
            "ok": True,
            "enabled": vault_enabled(),
            "pin_set": bool(r["pin_hash"]),
            "unlocked": int(r["unlocked_until"] or 0) > _vault_now(),
            "expires": int(r["unlocked_until"] or 0),
            "ttl_sec": int(VAULT_SESSION_TTL_SEC),
        })
    finally:
        conn.close()

@app.post("/vault/pin/set")
def vault_pin_set():
    if not vault_enabled():
        return jsonify({"ok": False, "error": "vault_not_configured"}), 400

    body = request.get_json(force=True, silent=True) or {}
    pin = str(body.get("pin") or "").strip()

    if len(pin) < 4 or len(pin) > 12 or (not pin.isdigit()):
        return jsonify({"ok": False, "error": "pin_must_be_4_to_12_digits"}), 400

    conn = get_conn()
    try:
        st = conn.execute("SELECT pin_hash FROM vault_state WHERE id=1").fetchone()
        pin_exists = bool(st["pin_hash"])
        if pin_exists and (not vault_unlocked()):
            return jsonify({"ok": False, "error": "vault_locked"}), 401

        conn.execute("UPDATE vault_state SET pin_hash=? WHERE id=1", (_format_pin_hash(pin),))
        conn.execute("UPDATE vault_state SET unlocked_until=? WHERE id=1", (_vault_now() + int(VAULT_SESSION_TTL_SEC),))
        conn.commit()
        return jsonify({"ok": True, "pin_set": True, "unlocked": True, "ttl_sec": int(VAULT_SESSION_TTL_SEC)})
    finally:
        conn.close()

@app.post("/vault/unlock")
def vault_unlock():
    if not vault_enabled():
        return jsonify({"ok": False, "error": "vault_not_configured"}), 400

    body = request.get_json(force=True, silent=True) or {}
    pin = str(body.get("pin") or "").strip()

    conn = get_conn()
    try:
        st = conn.execute("SELECT pin_hash FROM vault_state WHERE id=1").fetchone()
        if not st["pin_hash"]:
            return jsonify({"ok": False, "error": "pin_not_set"}), 400
        if not _verify_pin(pin, st["pin_hash"]):
            return jsonify({"ok": False, "error": "bad_pin"}), 401

        exp = _vault_now() + int(VAULT_SESSION_TTL_SEC)
        conn.execute("UPDATE vault_state SET unlocked_until=? WHERE id=1", (exp,))
        conn.commit()
        return jsonify({"ok": True, "unlocked": True, "expires": exp, "ttl_sec": int(VAULT_SESSION_TTL_SEC)})
    finally:
        conn.close()

@app.post("/vault/lock")
def vault_lock():
    if not vault_enabled():
        return jsonify({"ok": False, "error": "vault_not_configured"}), 400
    conn = get_conn()
    try:
        conn.execute("UPDATE vault_state SET unlocked_until=0 WHERE id=1")
        conn.commit()
        return jsonify({"ok": True, "unlocked": False})
    finally:
        conn.close()

@app.get("/vault/keys")
def vault_keys_list():
    gate = _require_unlocked()
    if gate:
        return gate

    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, exchange, enc_key, nonce, created FROM vault_keys ORDER BY id DESC LIMIT 200"
        ).fetchall()

        out = []
        for r in rows:
            try:
                raw = _vault_decrypt_value(r["enc_key"], r["nonce"])
                obj = json.loads(raw)
                out.append({
                    "id": int(r["id"]),
                    "exchange": (r["exchange"] or obj.get("exchange") or ""),
                    "created": r["created"] or "",
                    "api_key_masked": _mask(obj.get("api_key")),
                    "has_secret": bool(obj.get("api_secret")),
                    "has_passphrase": bool(obj.get("passphrase")),
                })
            except Exception:
                out.append({
                    "id": int(r["id"]),
                    "exchange": r["exchange"] or "",
                    "created": r["created"] or "",
                    "api_key_masked": "****",
                    "has_secret": True,
                    "has_passphrase": False,
                    "error": "decrypt_failed",
                })

        return jsonify({"ok": True, "count": len(out), "keys": out})
    finally:
        conn.close()

@app.post("/vault/keys/add")
def vault_keys_add():
    gate = _require_unlocked()
    if gate:
        return gate

    body = request.get_json(force=True, silent=True) or {}
    exchange = str(body.get("exchange") or "").strip().upper()
    api_key = str(body.get("api_key") or "").strip()
    api_secret = str(body.get("api_secret") or "").strip()
    passphrase = str(body.get("passphrase") or "").strip()

    if not exchange:
        return jsonify({"ok": False, "error": "exchange_required"}), 400
    if not api_key or not api_secret:
        return jsonify({"ok": False, "error": "api_key_and_secret_required"}), 400

    payload = {"exchange": exchange, "api_key": api_key, "api_secret": api_secret, "passphrase": passphrase or ""}
    enc, nonce = _vault_encrypt_value(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    created = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO vault_keys(exchange, enc_key, nonce, created) VALUES(?,?,?,?)",
            (exchange, enc, nonce, created),
        )
        conn.commit()
        new_id = cur.lastrowid
    finally:
        conn.close()

    return jsonify({"ok": True, "id": int(new_id), "exchange": exchange, "created": created})

@app.delete("/vault/keys/delete/<int:key_id>")
def vault_keys_delete(key_id: int):
    gate = _require_unlocked()
    if gate:
        return gate
    conn = get_conn()
    try:
        conn.execute("DELETE FROM vault_keys WHERE id=?", (int(key_id),))
        conn.commit()
        return jsonify({"ok": True, "deleted": int(key_id)})
    finally:
        conn.close()

@app.get("/vault/guardrails")
def vault_guardrails():
    return jsonify({
        "ok": True,
        "withdrawals_supported": False,
        "ttl_sec": int(VAULT_SESSION_TTL_SEC),
    })

@app.get("/health")
def health():
    return jsonify({"ok": True, "time_utc": utc_now_iso()})

@app.get("/signal")
def signal():
    market = request.args.get("market", "BTCUSDT")
    interval = int(request.args.get("interval", "60"))
    return jsonify(build_signal(market=market, interval_sec=interval))

@app.get("/decision")
def decision():
    market = request.args.get("market", "BTCUSDT")
    interval = int(request.args.get("interval", "60"))
    return jsonify(build_decision(market=market, interval_sec=interval))

@app.get("/decision/best")
def decision_best():
    interval = int(request.args.get("interval", "60"))
    return jsonify(build_best_decision(interval_sec=interval))

@app.get("/ohlc")
def ohlc():
    market = request.args.get("market", "BTCUSDT")
    interval = int(request.args.get("interval", "60"))
    limit = int(request.args.get("limit", "200"))
    return jsonify({"market": market, "interval_sec": interval, "candles": compute_ohlc(market, interval, limit)})

@app.get("/paper/state")
def paper_state():
    _paper_update_equity()
    return jsonify({
        "cash_usd": PAPER.cash_usd,
        "equity_usd": PAPER.equity_usd,
        "peak_equity_usd": PAPER.peak_equity_usd,
        "drawdown_pct": PAPER.drawdown_pct,
        "position": asdict(PAPER.position) if PAPER.position else None,
        "trades": len(PAPER_TRADES),
        "cfg": asdict(PAPER_CFG),
        "trade_mode": get_settings_public().get("trade_mode", "PAPER"),
    })

@app.get("/paper/trades")
def paper_trades():
    return jsonify({"trades": PAPER_TRADES[-200:]})

@app.post("/paper/reset")
def paper_reset():
    body = request.get_json(silent=True) or {}
    start = float(body.get("start_cash_usd") or 1000.0)
    PAPER.cash_usd = start
    PAPER.equity_usd = start
    PAPER.peak_equity_usd = start
    PAPER.drawdown_pct = 0.0
    PAPER.position = None
    PAPER_TRADES.clear()
    return jsonify({"ok": True, "start_cash_usd": start})

@app.post("/paper/tick")
def paper_tick():
    if (get_settings_public().get("trade_mode") or "PAPER").upper() != "PAPER":
        return jsonify({"ok": False, "error": "trade_mode_locked_to_paper"}), 403

    exit_result = _paper_check_exit()
    best_wrap = build_best_decision(interval_sec=60)
    best = (best_wrap or {}).get("best") or {}

    opened = None
    if best.get("action") in ("BUY", "SELL") and bool(best.get("eligible", True)):
        opened = _paper_open_from_decision(best)

    _paper_update_equity()
    return jsonify({
        "ok": True,
        "exit": exit_result,
        "best": best,
        "open": opened,
        "state": {
            "cash_usd": PAPER.cash_usd,
            "equity_usd": PAPER.equity_usd,
            "drawdown_pct": PAPER.drawdown_pct,
            "position": asdict(PAPER.position) if PAPER.position else None,
        }
    })

@app.get("/settings")
def get_settings_route():
    return jsonify(get_settings_public())

@app.post("/settings")
def set_settings_route():
    body = request.get_json(force=True, silent=True) or {}
    s = load_settings()

    if "bankroll_gbp" in body:
        s["bankroll_gbp"] = max(0.0, float(body.get("bankroll_gbp", 0)))
    elif "bankroll_usd" in body:
        bankroll_usd = float(body.get("bankroll_usd", 0))
        s["bankroll_gbp"] = max(0.0, (bankroll_usd / GBPUSD_RATE) if GBPUSD_RATE else 0.0)

    for k in [
        "risk_per_trade_pct", "max_open_positions", "min_trade_interval_sec",
        "atr_period", "atr_stop_mult", "min_notional_usd", "max_notional_usd",
        "best_max_markets", "best_min_confidence",
    ]:
        if k in body:
            s[k] = body.get(k)

    save_settings(s)
    return jsonify({"ok": True, **get_settings_public()})

@app.get("/control")
def control_get():
    return jsonify(get_control())

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
    return jsonify({"ok": True, "state": "ACTIVE"})

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
    conn = get_conn()
    try:
        for market, price in prices.items():
            try:
                m = (str(market) or "").strip().upper()
                if not m:
                    continue
                conn.execute(
                    "INSERT INTO prices(time_utc,time_epoch,market,price) VALUES(?,?,?,?)",
                    (time_utc, time_epoch, m, float(price))
                )
                count += 1
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()

    return jsonify({"ok": True, "count": count})

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

    if hb:
        hb["markets"] = _safe_markets_list(_safe_json_loads(hb.get("markets")) or hb.get("markets"))

    _paper_update_equity()

    return jsonify({
        "control": ctrl,
        "state": state,
        "heartbeat": hb or {},
        "pet": pet or {},
        "equity": [{"equity_usd": float(p["equity_usd"]), "time_utc": p["time_utc"]} for p in equity_points],
        "trades": recent_trades,
        "prices": latest_prices,
        "events": events,
        "settings": get_settings_public(),
        "paper": {
            "cash_usd": PAPER.cash_usd,
            "equity_usd": PAPER.equity_usd,
            "drawdown_pct": PAPER.drawdown_pct,
            "position": asdict(PAPER.position) if PAPER.position else None,
        },
        "vault": {"enabled": vault_enabled(), "unlocked": vault_unlocked()},
    })

@app.get("/")
def home():
    return jsonify({
        "ok": True,
        "service": "crypto-ai-api",
        "time_utc": utc_now_iso(),
        "vault_enabled": vault_enabled(),
        "vault_unlocked": vault_unlocked(),
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

# ===== PART 2 END =====

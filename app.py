import os
import json
import sqlite3
import math
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List
import time
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


# --- Paper trading state (in-memory) ---
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


# --- Paper trading config ---
@dataclass
class PaperConfig:
    enabled: bool = True
    fee_bps: float = 4.0           # 0.04% each side
    slippage_bps: float = 3.0      # 0.03% adverse
    max_drawdown_pct: float = 12.0 # kill-switch
    rr_takeprofit: float = 1.5     # 1.5R take profit (0 disables)
    allow_shorts: bool = True
    one_position_only: bool = True

PAPER_CFG = PaperConfig()

PAPER = PaperState()
PAPER_TRADES: List[Dict[str, Any]] = []
PAPER = PaperState()
PAPER_TRADES: List[Dict[str, Any]] = []

def _paper_mark_price(market: str) -> float:
    # Use your existing candles function to get the latest close as "mark"
    candles = compute_ohlc(market=market, interval_sec=60, limit=2)
    return float(candles[-1]["c"]) if candles else 0.0

def _paper_update_equity():
    PAPER.equity_usd = PAPER.cash_usd
    if PAPER.position:
        mark = _paper_mark_price(PAPER.position.market)
        if mark > 0:
            pnl = 0.0
            if PAPER.position.side == "LONG":
                pnl = (mark - PAPER.position.entry) * PAPER.position.qty
            else:
                pnl = (PAPER.position.entry - mark) * PAPER.position.qty
            PAPER.equity_usd += pnl

    PAPER.peak_equity_usd = max(PAPER.peak_equity_usd, PAPER.equity_usd)
    if PAPER.peak_equity_usd > 0:
        PAPER.drawdown_pct = max(0.0, (PAPER.peak_equity_usd - PAPER.equity_usd) / PAPER.peak_equity_usd * 100.0)

def _paper_open_from_decision(decision: Dict[str, Any]) -> Dict[str, Any]:
    """
    decision is your existing build_decision/best output.
    Expects: market, action BUY/SELL, size_usd, stop_distance, features.entry
    """
    market = decision["market"]
    action = decision["action"]
    size_usd = float(decision.get("size_usd") or 0.0)
    stop_distance = float(decision.get("stop_distance") or 0.0)
    entry = float((decision.get("features") or {}).get("entry") or 0.0)

    if entry <= 0 or size_usd <= 0 or stop_distance <= 0:
        return {"ok": False, "why": "bad_decision_inputs"}

    # Paper rule: only one open position
    if PAPER.position is not None:
        return {"ok": False, "why": "position_already_open"}

    side = "LONG" if action == "BUY" else "SHORT"
    qty = size_usd / entry

    # Simple stop placement
    if side == "LONG":
        stop = entry - stop_distance
    else:
        stop = entry + stop_distance

    PAPER.position = PaperPosition(
        market=market,
        side=side,
        qty=float(qty),
        entry=float(entry),
        stop=float(stop),
        opened_ts=int(time.time()),
    )

    PAPER_TRADES.append({
        "ts": PAPER.position.opened_ts,
        "type": "OPEN",
        "market": market,
        "side": side,
        "qty": float(qty),
        "entry": float(entry),
        "stop": float(stop),
        "meta": {
            "confidence": decision.get("confidence"),
            "reason": decision.get("reason"),
            "stop_distance": stop_distance,
            "size_usd": size_usd,
        }
    })

    # Assume no margin; just reduce cash by notional (spot-style paper)
    fee = _paper_fee(size_usd)
    PAPER.cash_usd -= (size_usd + fee)
    _paper_update_equity()
    return {"ok": True, "opened": asdict(PAPER.position)}

def _paper_check_stop() -> Optional[Dict[str, Any]]:
    if not PAPER.position:
        return None

    mark = _paper_mark_price(PAPER.position.market)
    if mark <= 0:
        return None

    pos = PAPER.position
    hit = False
    if pos.side == "LONG" and mark <= pos.stop:
        hit = True
    if pos.side == "SHORT" and mark >= pos.stop:
        hit = True

    if not hit:
        return None

    # Close at mark
    pnl = 0.0
    if pos.side == "LONG":
        pnl = (mark - pos.entry) * pos.qty
    else:
        pnl = (pos.entry - mark) * pos.qty

    # Return cash (notional) + pnl
    notional = pos.entry * pos.qty
    PAPER.cash_usd += notional + pnl

    PAPER_TRADES.append({
        "ts": int(time.time()),
        "type": "CLOSE",
        "market": pos.market,
        "side": pos.side,
        "qty": pos.qty,
        "exit": float(mark),
        "pnl": float(pnl),
        "reason": "STOP_HIT",
        "stop": pos.stop,
        "entry": pos.entry,
    })

    PAPER.position = None
    _paper_update_equity()
    return {"ok": True, "closed_at": float(mark), "pnl": float(pnl)}
# ----------------------------
# Settings (bankroll + brain controls)
# ----------------------------
SETTINGS_PATH = os.getenv("SETTINGS_PATH", "/var/data/settings.json")
GBPUSD_RATE = float(os.getenv("GBPUSD_RATE", "1.27"))  # simple fixed rate

DEFAULT_SETTINGS = {
    "bankroll_gbp": 100.0,

    # Brain v1 risk controls (more active but still sane)
    "risk_per_trade_pct": 1.0,       # was 0.5
    "max_open_positions": 1,         # keep 1 for safety
    "min_trade_interval_sec": 20,    # was 60 (faster decisions)
    "atr_period": 14,
    "atr_stop_mult": 1.35,      # was 1.8 (slightly tighter stop)
    "min_notional_usd": 20.0,        # was 25 (easier to place trades on small bankroll)
    "max_notional_usd": 900.0,       # was 500 (allows bigger sizing if bankroll grows)

    # /decision/best controls (scan more + allow a trade more often)
    "best_max_markets": 20,          # was 8
    "best_min_confidence": 0.45,     # was 0.62
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
    """
]

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
# Helpers: fetch/insert
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


# ----------------------------
# Control helpers
# ----------------------------
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
# OHLC aggregation
# ----------------------------
def compute_ohlc(market: str, interval_sec: int = 60, limit: int = 200):
    market = (market or "").strip().upper()
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
# Brain v1: EMA + RSI + ATR sizing
# ==========================================================
SIGNAL_COOLDOWN_SEC = int(os.getenv("SIGNAL_COOLDOWN_SEC", "30"))
_LAST_SIGNAL = {"time_epoch": 0, "market": "", "side": "hold", "confidence": 0.5, "reason": "init", "features": {}}
_LAST_DECISION_BY_MARKET = {}


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
        out = {"market": market, "side": "hold", "confidence": 0.50, "reason": "not_enough_data",
               "features": {"closes": len(closes), "interval_sec": interval_sec}}
        _LAST_SIGNAL.update({"time_epoch": now_epoch, **out})
        return out

    ema_fast = _ema(closes, 12)
    ema_slow = _ema(closes, 26)
    rsi14 = _rsi(closes, 14)

    if ema_fast is None or ema_slow is None or rsi14 is None:
        out = {"market": market, "side": "hold", "confidence": 0.50, "reason": "indicator_nan",
               "features": {"ema_fast": ema_fast, "ema_slow": ema_slow, "rsi14": rsi14,
                            "closes": len(closes), "interval_sec": interval_sec}}
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
            "features": {"interval_sec": interval_sec,
                         "cooldown_remaining_sec": max(0, min_gap - (now_epoch - int(last.get("time_epoch"))))},
        }

    sig = build_signal(market=market, interval_sec=interval_sec)

    candles = compute_ohlc(market=market, interval_sec=interval_sec, limit=260)
    if not candles or len(candles) < 25:
        return {"market": market, "action": "HOLD", "confidence": 0.0, "reason": "no_candles",
                "size_usd": 0.0, "stop_distance": 0.0, "features": {"interval_sec": interval_sec}}

    entry = float(candles[-1]["c"])

    atr_period = int(settings_public.get("atr_period") or DEFAULT_SETTINGS["atr_period"])
    atr = _atr(candles, period=atr_period)
    if atr is None or not (atr > 0):
        return {"market": market, "action": "HOLD", "confidence": 0.0, "reason": "atr_nan",
                "size_usd": 0.0, "stop_distance": 0.0, "features": {"interval_sec": interval_sec, "entry": entry}}

    stop_mult = float(settings_public.get("atr_stop_mult") or DEFAULT_SETTINGS["atr_stop_mult"])
    stop_distance = float(atr * stop_mult)

    side = (sig.get("side") or "hold").lower()
    conf = float(sig.get("confidence") or 0.0)
    reason = str(sig.get("reason") or "no_reason")

    min_conf = float(settings_public.get("best_min_confidence", 0.62))
    if side == "hold" and conf < min_conf:
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

    add_event(
        "decision",
        f"{market} {action} ${size_usd:.0f} ({conf:.2f})",
        {"reason": reason, "entry": entry, "atr": float(atr), "stop_distance": stop_distance, "risk": sizing_meta},
    )

    _LAST_DECISION_BY_MARKET[market] = {"time_epoch": now_epoch, "action": action}
    return out


def _list_candidate_markets(max_markets: int = 8):
    """
    Priority:
      1) latest heartbeat.markets (if present)
      2) distinct markets from latest prices (recent)
    """
    max_markets = max(1, min(50, int(max_markets)))

    hb = fetch_one("heartbeat")
    if hb:
        mk = _safe_markets_list(_safe_json_loads(hb.get("markets")) or hb.get("markets"))
        mk = [m for m in mk if m]
        if mk:
            return mk[:max_markets]

    # fallback: last N price rows, unique markets in order
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT market FROM prices ORDER BY time_epoch DESC LIMIT 2000"
        ).fetchall()
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
    """
    Simple ranking:
      - prioritize non-HOLD
      - confidence dominates
      - larger notional is a mild tiebreaker
    """
    if not isinstance(decision_obj, dict):
        return 0.0
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

        item = {
            "market": m,
            "action": action,
            "confidence": conf,
            "reason": d.get("reason") or "",
            "size_usd": float(d.get("size_usd") or 0.0),
            "stop_distance": float(d.get("stop_distance") or 0.0),
            "eligible": bool(eligible),
            "score": float(_best_score(d)) if eligible else 0.0,
        }
        candidates.append(item)

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
        },
        "candidates": candidates,
    }


# ----------------------------
# Base routes
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
            "GET": [
                "/", "/health", "/schema",
                "/signal", "/decision", "/decision/best",
                "/data", "/heartbeat", "/pet", "/events", "/logs",
                "/equity", "/trades", "/prices", "/ohlc", "/deaths", "/control", "/settings"
            ],
            "POST": [
                "/ingest/heartbeat", "/ingest/pet", "/ingest/event", "/ingest/equity", "/ingest/trade",
                "/ingest/prices", "/ingest/death",
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


# ----------------------------
# Data routes (your existing dashboard feeds)
# ----------------------------
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
        m = (p.get("market") or "").strip().upper()
        if m and m not in latest_by_market:
            p["market"] = m
            latest_by_market[m] = p

    return jsonify({
        "control": ctrl,
        "state": state,
        "heartbeat": hb or {},
        "pet": pet or {},
        "equity": [{"equity_usd": float(p["equity_usd"]), "time_utc": p["time_utc"]} for p in equity_points],
        "trades": [
            {
                "time_utc": t.get("time_utc", ""),
                "market": (t.get("market", "") or "").upper(),
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
        "settings": get_settings_public(),
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
@app.get("/paper/state")
def paper_state():
    _paper_update_equity()
    return {
        "cash_usd": PAPER.cash_usd,
        "equity_usd": PAPER.equity_usd,
        "peak_equity_usd": PAPER.peak_equity_usd,
        "drawdown_pct": PAPER.drawdown_pct,
        "position": asdict(PAPER.position) if PAPER.position else None,
        "trades": len(PAPER_TRADES),
    }

@app.get("/paper/trades")
def paper_trades():
    return {"trades": PAPER_TRADES[-200:]}  # last 200

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
    return {"ok": True, "start_cash_usd": start}

@app.post("/paper/tick")
def paper_tick():
    """
    1) update stop
    2) get best decision (your existing logic)
    3) if BUY/SELL eligible and no position -> open paper position
    """
    stop_result = _paper_check_stop()

    # Call your existing logic. If you already have a /decision/best function, reuse it.
    # Otherwise call the same function used by that endpoint.
    decision = build_best_decision(interval_sec=60) if "build_best_decision" in globals() else None
    if decision is None:
        # fallback: just do one market
        decision = build_decision(market="BTCUSDT", interval_sec=60)

    opened = None
    if decision.get("action") in ("BUY", "SELL") and bool(decision.get("eligible", True)):
        opened = _paper_open_from_decision(decision)

    _paper_update_equity()
    return {
        "ok": True,
        "stop": stop_result,
        "decision": decision,
        "open": opened,
        "state": {
            "cash_usd": PAPER.cash_usd,
            "equity_usd": PAPER.equity_usd,
            "drawdown_pct": PAPER.drawdown_pct,
            "position": asdict(PAPER.position) if PAPER.position else None,
        }
}

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
# Settings routes
# ----------------------------
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
        "risk_per_trade_pct",
        "max_open_positions",
        "min_trade_interval_sec",
        "atr_period",
        "atr_stop_mult",
        "min_notional_usd",
        "max_notional_usd",
        "best_max_markets",
        "best_min_confidence",
    ]:
        if k in body:
            s[k] = body.get(k)

    save_settings(s)
    out = get_settings_public()
    add_event("info", "Settings updated", out)
    return jsonify({"ok": True, **out})


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
        "market": (body.get("market", "BTCUSDT") or "BTCUSDT").upper(),
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
            m = (str(market) or "").strip().upper()
            if not m:
                continue
            insert_row("prices", {"time_utc": time_utc, "time_epoch": time_epoch, "market": m, "price": float(price)})
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

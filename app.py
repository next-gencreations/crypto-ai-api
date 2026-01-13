import os
import json
import sqlite3
import math
import time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List

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

# ==========================================================
# Paper Trading (in-memory)
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
    fee_bps: float = 4.0            # 0.04% each side
    slippage_bps: float = 3.0       # 0.03% adverse
    max_drawdown_pct: float = 12.0  # kill-switch
    rr_takeprofit: float = 1.5      # 1.5R take profit (0 disables)
    allow_shorts: bool = True
    one_position_only: bool = True


PAPER_CFG = PaperConfig()
PAPER = PaperState()
PAPER_TRADES: List[Dict[str, Any]] = []


# ----------------------------
# Paper helpers
# ----------------------------
def _bps(x: float) -> float:
    return float(x) / 10000.0


def _paper_fee(notional: float) -> float:
    return abs(float(notional)) * _bps(PAPER_CFG.fee_bps)


def _paper_apply_slippage(price: float, side: str, is_entry: bool) -> float:
    slip = _bps(PAPER_CFG.slippage_bps)
    side = (side or "").upper()
    if side == "LONG":
        # adverse: entry worse (higher), exit worse (lower)
        return price * (1.0 + slip) if is_entry else price * (1.0 - slip)
    else:
        # adverse for SHORT: entry worse (lower), exit worse (higher)
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

    # stop
    if pos.side == "LONG" and mark <= pos.stop:
        return _paper_close_position(mark, "STOP_HIT")
    if pos.side == "SHORT" and mark >= pos.stop:
        return _paper_close_position(mark, "STOP_HIT")

    # take profit
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


# ==========================================================
# Database config
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
            "features": {"ema_fast": ema_fast, "ema_slow": ema_slow, "rsi14": rsi14,
                         "closes": len(closes), "interval_sec": interval_sec}
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

    if bankroll_usd <= 0 or entry_price <= 0 or stop

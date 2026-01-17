import os
import json
import sqlite3
import math
import time
import base64
import secrets
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List

from flask import Flask, request, jsonify
from flask_cors import CORS

# ---- Vault crypto / auth deps ----
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

# WebAuthn (Passkeys)
from webauthn import (
    generate_registration_options,
    generate_authentication_options,
    verify_registration_response,
    verify_authentication_response,
)
from webauthn.helpers.structs import (
    RegistrationCredential,
    AuthenticationCredential,
    UserVerificationRequirement,
)

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

    # Hard safety: trading mode (PAPER only for now)
    "trade_mode": "PAPER",  # PAPER | TESTNET | LIVE (LIVE remains locked)
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
# Schema (includes Vault tables)
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
    # ---------------- Vault tables ----------------
    """
    CREATE TABLE IF NOT EXISTS vault_webauthn (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      created_time_utc TEXT NOT NULL,
      credential_id_b64 TEXT NOT NULL,
      public_key_b64 TEXT NOT NULL,
      sign_count INTEGER DEFAULT 0,
      transports TEXT DEFAULT '[]'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vault_challenges (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      created_time_utc TEXT NOT NULL,
      purpose TEXT NOT NULL,
      challenge_b64 TEXT NOT NULL,
      expires_epoch INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vault_pin (
      id INTEGER PRIMARY KEY CHECK (id = 1),
      created_time_utc TEXT NOT NULL,
      pin_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vault_sessions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      created_time_utc TEXT NOT NULL,
      token_hash TEXT NOT NULL,
      expires_epoch INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vault_keys (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      created_time_utc TEXT NOT NULL,
      exchange TEXT NOT NULL,
      label TEXT DEFAULT '',
      key_hint TEXT NOT NULL,
      cipher_b64 TEXT NOT NULL
    )
    """,
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
    # vault tables are created fresh; sqlite ALTER isn’t required unless you change them later
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
            "features": {
                "interval_sec": interval_sec,
                "entry": entry,
                "atr": float(atr),
                "sig": sig,
                "sizing": sizing_meta,
            },
        }

    action = "BUY" if side == "buy" else "SELL"
    out = {
        "market": market,
        "action": action,
        "confidence": conf,
        "reason": reason,
        "size_usd": float(size_usd),
        "stop_distance": float(stop_distance),
        "features": {
            "interval_sec": interval_sec,
            "entry": entry,
            "atr": float(atr),
            "sig": sig,
            "sizing": sizing_meta,
        },
    }

    add_event(
        "decision",
        f"{market} {action} ${size_usd:.0f} ({conf:.2f})",
        {"reason": reason, "entry": entry, "atr": float(atr), "stop_distance": stop_distance, "risk": sizing_meta},
    )

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
            "features": d.get("features") or {},
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
            "features": {},
        },
        "candidates": candidates,
    }

# ==========================================================
# VAULT (Passkeys + PIN fallback + encrypted keys)
# ==========================================================
VAULT_MASTER_KEY_B64 = os.getenv("VAULT_MASTER_KEY", "").strip()
VAULT_SETUP_TOKEN = os.getenv("VAULT_SETUP_TOKEN", "").strip()  # bootstrap for first pin/passkey if needed

RP_ID = os.getenv("RP_ID", "").strip()            # e.g. "crypto-ai-dashboard-delta.vercel.app" or your custom domain
RP_ORIGIN = os.getenv("RP_ORIGIN", "").strip()    # e.g. "https://crypto-ai-dashboard-delta.vercel.app"
VAULT_USER_ID = os.getenv("VAULT_USER_ID", "vault-user").strip()
VAULT_DISPLAY_NAME = os.getenv("VAULT_DISPLAY_NAME", "Vault Owner").strip()
VAULT_UNLOCK_TTL_SEC = int(os.getenv("VAULT_UNLOCK_TTL_SEC", "300"))  # 5 min

_ph = PasswordHasher(time_cost=2, memory_cost=102400, parallelism=8)

def _vault_enabled() -> bool:
    return bool(VAULT_MASTER_KEY_B64 and RP_ID and RP_ORIGIN)

def _vault_master_key_bytes() -> bytes:
    # Must be 32 bytes base64
    raw = base64.b64decode(VAULT_MASTER_KEY_B64.encode("utf-8"))
    if len(raw) != 32:
        raise ValueError("VAULT_MASTER_KEY must decode to 32 bytes")
    return raw

def _sha256_b64(s: str) -> str:
    import hashlib
    h = hashlib.sha256(s.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(h).decode("utf-8").rstrip("=")

def _require_setup_or_unlocked():
    # For endpoints like pin set / passkey register: allow if setup token is correct OR vault already unlocked
    token = (request.headers.get("X-VAULT-SETUP") or "").strip()
    if token and VAULT_SETUP_TOKEN and secrets.compare_digest(token, VAULT_SETUP_TOKEN):
        return True
    # otherwise require unlocked
    return _is_vault_unlocked()

def _get_vault_token() -> str:
    return (request.headers.get("X-VAULT-TOKEN") or "").strip()

def _is_vault_unlocked() -> bool:
    token = _get_vault_token()
    if not token:
        return False
    th = _sha256_b64(token)

    now_epoch = int(time.time())
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, expires_epoch FROM vault_sessions WHERE token_hash=? ORDER BY id DESC LIMIT 1",
            (th,)
        ).fetchone()
        if not row:
            return False
        return int(row["expires_epoch"]) > now_epoch
    finally:
        conn.close()

def _issue_vault_session() -> Dict[str, Any]:
    token = secrets.token_urlsafe(32)
    th = _sha256_b64(token)
    expires = int(time.time()) + VAULT_UNLOCK_TTL_SEC
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO vault_sessions (created_time_utc, token_hash, expires_epoch) VALUES (?, ?, ?)",
            (utc_now_iso(), th, expires)
        )
        conn.commit()
    finally:
        conn.close()
    return {"vault_token": token, "expires_epoch": expires, "ttl_sec": VAULT_UNLOCK_TTL_SEC}

def _vault_encrypt_json(payload: dict) -> str:
    key = _vault_master_key_bytes()
    aes = AESGCM(key)
    nonce = secrets.token_bytes(12)
    pt = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ct = aes.encrypt(nonce, pt, None)  # includes auth tag
    blob = nonce + ct
    return base64.urlsafe_b64encode(blob).decode("utf-8")

def _vault_decrypt_json(cipher_b64: str) -> dict:
    key = _vault_master_key_bytes()
    aes = AESGCM(key)
    blob = base64.urlsafe_b64decode(cipher_b64.encode("utf-8"))
    nonce = blob[:12]
    ct = blob[12:]
    pt = aes.decrypt(nonce, ct, None)
    return json.loads(pt.decode("utf-8"))

def _mask_key(s: str) -> str:
    s = (s or "").strip()
    if len(s) <= 8:
        return s[:2] + "…" + s[-2:]
    return s[:4] + "…" + s[-4:]

def _cleanup_expired_vault_challenges_and_sessions():
    now = int(time.time())
    conn = get_conn()
    try:
        conn.execute("DELETE FROM vault_challenges WHERE expires_epoch <= ?", (now,))
        conn.execute("DELETE FROM vault_sessions WHERE expires_epoch <= ?", (now,))
        conn.commit()
    finally:
        conn.close()

def _vault_has_passkey() -> bool:
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM vault_webauthn ORDER BY id DESC LIMIT 1").fetchone()
        return row is not None
    finally:
        conn.close()

def _vault_has_pin() -> bool:
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM vault_pin WHERE id=1").fetchone()
        return row is not None
    finally:
        conn.close()

def _save_challenge(purpose: str, challenge_bytes: bytes, ttl_sec: int = 180):
    expires = int(time.time()) + int(ttl_sec)
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO vault_challenges (created_time_utc, purpose, challenge_b64, expires_epoch) VALUES (?, ?, ?, ?)",
            (utc_now_iso(), purpose, base64.urlsafe_b64encode(challenge_bytes).decode("utf-8"), expires)
        )
        conn.commit()
    finally:
        conn.close()

def _pop_challenge(purpose: str) -> Optional[bytes]:
    now = int(time.time())
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, challenge_b64, expires_epoch FROM vault_challenges WHERE purpose=? ORDER BY id DESC LIMIT 1",
            (purpose,)
        ).fetchone()
        if not row:
            return None
        if int(row["expires_epoch"]) <= now:
            return None
        conn.execute("DELETE FROM vault_challenges WHERE id=?", (int(row["id"]),))
        conn.commit()
        return base64.urlsafe_b64decode(row["challenge_b64"].encode("utf-8"))
    finally:
        conn.close()

# ----------------------------
# Vault endpoints
# ----------------------------
@app.get("/vault/status")
def vault_status():
    _cleanup_expired_vault_challenges_and_sessions()
    return jsonify({
        "ok": True,
        "enabled": _vault_enabled(),
        "has_passkey": _vault_has_passkey(),
        "has_pin": _vault_has_pin(),
        "unlocked": _is_vault_unlocked(),
        "ttl_sec": VAULT_UNLOCK_TTL_SEC,
    })

@app.post("/vault/webauthn/register/options")
def vault_register_options():
    if not _vault_enabled():
        return jsonify({"ok": False, "error": "vault_not_configured"}), 400
    if not _require_setup_or_unlocked():
        return jsonify({"ok": False, "error": "requires_setup_token_or_unlock"}), 401

    # Create registration options
    options = generate_registration_options(
        rp_id=RP_ID,
        rp_name="Crypto AI Vault",
        user_id=VAULT_USER_ID.encode("utf-8"),
        user_name=VAULT_USER_ID,
        user_display_name=VAULT_DISPLAY_NAME,
        timeout=60000,
        user_verification=UserVerificationRequirement.PREFERRED,
    )

    # Store challenge
    _save_challenge("register", options.challenge)

    return jsonify({
        "ok": True,
        "publicKey": json.loads(options.json),
        "rp_id": RP_ID,
        "rp_origin": RP_ORIGIN,
    })

@app.post("/vault/webauthn/register/verify")
def vault_register_verify():
    if not _vault_enabled():
        return jsonify({"ok": False, "error": "vault_not_configured"}), 400
    if not _require_setup_or_unlocked():
        return jsonify({"ok": False, "error": "requires_setup_token_or_unlock"}), 401

    body = request.get_json(force=True, silent=True) or {}
    challenge = _pop_challenge("register")
    if not challenge:
        return jsonify({"ok": False, "error": "missing_or_expired_challenge"}), 400

    try:
        cred = RegistrationCredential.parse_raw(json.dumps(body))
        verification = verify_registration_response(
            credential=cred,
            expected_challenge=challenge,
            expected_rp_id=RP_ID,
            expected_origin=RP_ORIGIN,
            require_user_verification=False,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": "register_verify_failed", "details": str(e)}), 400

    # Persist credential
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO vault_webauthn
            (created_time_utc, credential_id_b64, public_key_b64, sign_count, transports)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                utc_now_iso(),
                base64.urlsafe_b64encode(verification.credential_id).decode("utf-8"),
                base64.urlsafe_b64encode(verification.credential_public_key).decode("utf-8"),
                int(verification.sign_count or 0),
                json.dumps(body.get("response", {}).get("transports", []) or []),
            )
        )
        conn.commit()
    finally:
        conn.close()

    add_event("info", "Vault passkey registered", {"rp_id": RP_ID})

    return jsonify({"ok": True})

@app.post("/vault/webauthn/login/options")
def vault_login_options():
    if not _vault_enabled():
        return jsonify({"ok": False, "error": "vault_not_configured"}), 400

    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT credential_id_b64 FROM vault_webauthn ORDER BY id DESC LIMIT 10"
        ).fetchall()
    finally:
        conn.close()

    allow_credentials = []
    for r in rows:
        try:
            cid = base64.urlsafe_b64decode(r["credential_id_b64"].encode("utf-8"))
            allow_credentials.append({"id": cid, "type": "public-key"})
        except Exception:
            pass

    options = generate_authentication_options(
        rp_id=RP_ID,
        timeout=60000,
        allow_credentials=allow_credentials or None,
        user_verification=UserVerificationRequirement.PREFERRED,
    )

    _save_challenge("login", options.challenge)

    return jsonify({"ok": True, "publicKey": json.loads(options.json), "rp_id": RP_ID, "rp_origin": RP_ORIGIN})

@app.post("/vault/webauthn/login/verify")
def vault_login_verify():
    if not _vault_enabled():
        return jsonify({"ok": False, "error": "vault_not_configured"}), 400

    body = request.get_json(force=True, silent=True) or {}
    challenge = _pop_challenge("login")
    if not challenge:
        return jsonify({"ok": False, "error": "missing_or_expired_challenge"}), 400

    # Find stored credential by ID
    try:
        cred = AuthenticationCredential.parse_raw(json.dumps(body))
        cred_id = cred.raw_id
    except Exception:
        return jsonify({"ok": False, "error": "bad_credential_payload"}), 400

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, credential_id_b64, public_key_b64, sign_count FROM vault_webauthn ORDER BY id DESC"
        ).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "no_passkey_registered"}), 400

        stored_cred_id = base64.urlsafe_b64decode(row["credential_id_b64"].encode("utf-8"))
        stored_pubkey = base64.urlsafe_b64decode(row["public_key_b64"].encode("utf-8"))
        stored_sign = int(row["sign_count"] or 0)

        # Basic safety: ensure user is authenticating with the stored credential
        if cred_id != stored_cred_id:
            return jsonify({"ok": False, "error": "credential_not_recognized"}), 401

        try:
            verification = verify_authentication_response(
                credential=cred,
                expected_challenge=challenge,
                expected_rp_id=RP_ID,
                expected_origin=RP_ORIGIN,
                credential_public_key=stored_pubkey,
                credential_current_sign_count=stored_sign,
                require_user_verification=False,
            )
        except Exception as e:
            return jsonify({"ok": False, "error": "login_verify_failed", "details": str(e)}), 401

        # Update sign count
        conn.execute("UPDATE vault_webauthn SET sign_count=? WHERE id=?", (int(verification.new_sign_count), int(row["id"])))
        conn.commit()
    finally:
        conn.close()

    add_event("info", "Vault unlocked (passkey)", {})
    return jsonify({"ok": True, **_issue_vault_session()})

@app.post("/vault/pin/set")
def vault_pin_set():
    if not _vault_enabled():
        return jsonify({"ok": False, "error": "vault_not_configured"}), 400
    if not _require_setup_or_unlocked():
        return jsonify({"ok": False, "error": "requires_setup_token_or_unlock"}), 401

    body = request.get_json(force=True, silent=True) or {}
    pin = str(body.get("pin") or "").strip()
    if not pin.isdigit() or len(pin) < 4 or len(pin) > 10:
        return jsonify({"ok": False, "error": "pin_must_be_4_to_10_digits"}), 400

    pin_hash = _ph.hash(pin)

    conn = get_conn()
    try:
        conn.execute("DELETE FROM vault_pin WHERE id=1")
        conn.execute(
            "INSERT INTO vault_pin (id, created_time_utc, pin_hash) VALUES (1, ?, ?)",
            (utc_now_iso(), pin_hash)
        )
        conn.commit()
    finally:
        conn.close()

    add_event("info", "Vault PIN set/rotated", {})
    return jsonify({"ok": True})

@app.post("/vault/pin/unlock")
def vault_pin_unlock():
    if not _vault_enabled():
        return jsonify({"ok": False, "error": "vault_not_configured"}), 400

    body = request.get_json(force=True, silent=True) or {}
    pin = str(body.get("pin") or "").strip()

    conn = get_conn()
    try:
        row = conn.execute("SELECT pin_hash FROM vault_pin WHERE id=1").fetchone()
        if not row:
            return jsonify({"ok": False, "error": "pin_not_set"}), 400
        pin_hash = row["pin_hash"]
    finally:
        conn.close()

    try:
        _ph.verify(pin_hash, pin)
    except VerifyMismatchError:
        return jsonify({"ok": False, "error": "bad_pin"}), 401
    except Exception:
        return jsonify({"ok": False, "error": "pin_verify_failed"}), 401

    add_event("info", "Vault unlocked (pin)", {})
    return jsonify({"ok": True, **_issue_vault_session()})

@app.post("/vault/lock")
def vault_lock():
    # Revokes all sessions (requires unlocked)
    if not _is_vault_unlocked():
        return jsonify({"ok": False, "error": "not_unlocked"}), 401
    conn = get_conn()
    try:
        conn.execute("DELETE FROM vault_sessions")
        conn.commit()
    finally:
        conn.close()
    add_event("warning", "Vault locked (sessions revoked)", {})
    return jsonify({"ok": True})

@app.get("/vault/keys")
def vault_keys_list():
    if not _vault_enabled():
        return jsonify({"ok": False, "error": "vault_not_configured"}), 400
    if not _is_vault_unlocked():
        return jsonify({"ok": False, "error": "not_unlocked"}), 401

    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, created_time_utc, exchange, label, key_hint FROM vault_keys ORDER BY id DESC"
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": int(r["id"]),
                "created_time_utc": r["created_time_utc"],
                "exchange": r["exchange"],
                "label": r["label"],
                "key_hint": r["key_hint"],
            })
        return jsonify({"ok": True, "keys": out})
    finally:
        conn.close()

@app.post("/vault/keys")
def vault_keys_save():
    """
    Save exchange API keys encrypted.
    IMPORTANT: Do NOT enable withdrawals on the exchange key.
    """
    if not _vault_enabled():
        return jsonify({"ok": False, "error": "vault_not_configured"}), 400
    if not _is_vault_unlocked():
        return jsonify({"ok": False, "error": "not_unlocked"}), 401

    body = request.get_json(force=True, silent=True) or {}
    exchange = (body.get("exchange") or "").strip().upper()
    label = (body.get("label") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    api_secret = (body.get("api_secret") or "").strip()
    passphrase = (body.get("passphrase") or "").strip()  # for some exchanges (optional)

    if not exchange or not api_key or not api_secret:
        return jsonify({"ok": False, "error": "missing_exchange_or_key_or_secret"}), 400

    payload = {
        "exchange": exchange,
        "api_key": api_key,
        "api_secret": api_secret,
        "passphrase": passphrase,
        "created_time_utc": utc_now_iso(),
        "permissions_note": "READ+TRADE only. WITHDRAW disabled.",
    }
    cipher_b64 = _vault_encrypt_json(payload)

    hint = _mask_key(api_key)

    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO vault_keys (created_time_utc, exchange, label, key_hint, cipher_b64) VALUES (?, ?, ?, ?, ?)",
            (utc_now_iso(), exchange, label, hint, cipher_b64)
        )
        conn.commit()
    finally:
        conn.close()

    add_event("info", "Vault key saved", {"exchange": exchange, "key_hint": hint})
    return jsonify({"ok": True, "exchange": exchange, "key_hint": hint})

@app.delete("/vault/keys/<int:key_id>")
def vault_keys_delete(key_id: int):
    if not _vault_enabled():
        return jsonify({"ok": False, "error": "vault_not_configured"}), 400
    if not _is_vault_unlocked():
        return jsonify({"ok": False, "error": "not_unlocked"}), 401

    conn = get_conn()
    try:
        conn.execute("DELETE FROM vault_keys WHERE id=?", (int(key_id),))
        conn.commit()
    finally:
        conn.close()

    add_event("warning", "Vault key deleted", {"id": int(key_id)})
    return jsonify({"ok": True})

@app.post("/vault/keys/test")
def vault_keys_test():
    """
    Safe server-side “test”: decrypts and validates shape only.
    (We can later add real exchange ping once you confirm which exchange library you want.)
    """
    if not _vault_enabled():
        return jsonify({"ok": False, "error": "vault_not_configured"}), 400
    if not _is_vault_unlocked():
        return jsonify({"ok": False, "error": "not_unlocked"}), 401

    body = request.get_json(force=True, silent=True) or {}
    key_id = int(body.get("id") or 0)
    if key_id <= 0:
        return jsonify({"ok": False, "error": "missing_id"}), 400

    conn = get_conn()
    try:
        row = conn.execute("SELECT id, exchange, cipher_b64, key_hint FROM vault_keys WHERE id=?", (key_id,)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "not_found"}), 404
        blob = _vault_decrypt_json(row["cipher_b64"])
    finally:
        conn.close()

    ok = bool(blob.get("api_key")) and bool(blob.get("api_secret")) and (blob.get("exchange") == row["exchange"])
    return jsonify({
        "ok": ok,
        "exchange": row["exchange"],
        "key_hint": row["key_hint"],
        "checks": {
            "has_api_key": bool(blob.get("api_key")),
            "has_api_secret": bool(blob.get("api_secret")),
            "exchange_match": (blob.get("exchange") == row["exchange"]),
        }
    })

@app.get("/vault/mode")
def vault_mode_get():
    # Public read (safe)
    return jsonify({"ok": True, "trade_mode": get_settings_public().get("trade_mode", "PAPER")})

@app.post("/vault/mode")
def vault_mode_set():
    """
    Trade mode setter.
    For now: HARD LOCK to PAPER unless you explicitly add a live unlock policy later.
    """
    if not _vault_enabled():
        return jsonify({"ok": False, "error": "vault_not_configured"}), 400
    if not _is_vault_unlocked():
        return jsonify({"ok": False, "error": "not_unlocked"}), 401

    body = request.get_json(force=True, silent=True) or {}
    mode = (body.get("trade_mode") or "PAPER").upper().strip()

    # Hard lock: only allow PAPER right now (exactly what you requested)
    if mode != "PAPER":
        return jsonify({"ok": False, "error": "live_and_testnet_locked", "allowed": ["PAPER"]}), 403

    s = load_settings()
    s["trade_mode"] = "PAPER"
    save_settings(s)
    add_event("info", "Trade mode set", {"trade_mode": "PAPER"})
    return jsonify({"ok": True, "trade_mode": "PAPER"})

# ==========================================================
# Base routes
# ==========================================================
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
        "vault": {
            "enabled": _vault_enabled(),
            "has_passkey": _vault_has_passkey(),
            "has_pin": _vault_has_pin(),
        },
        "endpoints": {
            "GET": [
                "/", "/health", "/schema",
                "/signal", "/decision", "/decision/best",
                "/paper/state", "/paper/trades",
                "/data", "/heartbeat", "/pet", "/events", "/logs",
                "/equity", "/trades", "/prices", "/ohlc", "/deaths", "/control", "/settings",
                "/vault/status", "/vault/keys", "/vault/mode"
            ],
            "POST": [
                "/paper/reset", "/paper/tick",
                "/ingest/heartbeat", "/ingest/pet", "/ingest/event", "/ingest/equity", "/ingest/trade",
                "/ingest/prices", "/ingest/death",
                "/control/pause", "/control/cryo", "/control/revive",
                "/settings",
                "/vault/webauthn/register/options", "/vault/webauthn/register/verify",
                "/vault/webauthn/login/options", "/vault/webauthn/login/verify",
                "/vault/pin/set", "/vault/pin/unlock",
                "/vault/keys", "/vault/keys/test",
                "/vault/lock",
                "/vault/mode"
            ],
            "DELETE": ["/reset/all", "/reset/events", "/reset/trades", "/reset/equity", "/reset/prices", "/reset/deaths", "/vault/keys/<id>"]
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
# Paper routes
# ----------------------------
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
    """
    1) check exits (stop/tp)
    2) get best decision
    3) if eligible BUY/SELL and no position -> open paper position

    SAFETY: even if you later add live endpoints, trade_mode is hard locked to PAPER by vault/mode.
    """
    # extra safety gate
    if (get_settings_public().get("trade_mode") or "PAPER").upper() != "PAPER":
        add_event("warning", "Trade blocked: mode not PAPER", {"trade_mode": get_settings_public().get("trade_mode")})
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

# ----------------------------
# Data routes (dashboard feeds)
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
        "paper": {
            "cash_usd": PAPER.cash_usd,
            "equity_usd": PAPER.equity_usd,
            "drawdown_pct": PAPER.drawdown_pct,
            "position": asdict(PAPER.position) if PAPER.position else None,
        },
        "vault": {
            "enabled": _vault_enabled(),
            "unlocked": _is_vault_unlocked(),
            "has_passkey": _vault_has_passkey(),
            "has_pin": _vault_has_pin(),
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

    # DO NOT allow setting trade_mode via public settings route
    # trade_mode changes must go through /vault/mode which is locked to PAPER
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

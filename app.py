import os
import json
import sqlite3
import math
import time
import base64
import secrets
import hashlib
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List

from flask import Flask, request, jsonify
from flask_cors import CORS

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


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

EXPECTED_COLUMNS = {
    "meta": {"schema_version": "INTEGER DEFAULT 1", "updated_time_utc": "TEXT DEFAULT ''"},
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
    "prices": {"time_utc": "TEXT DEFAULT ''", "time_epoch": "INTEGER DEFAULT 0", "market": "TEXT DEFAULT ''", "price": "REAL DEFAULT 0"},
    "equity": {"time_utc": "TEXT DEFAULT ''", "time_epoch": "INTEGER DEFAULT 0", "equity_usd": "REAL DEFAULT 0"},
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
    "events": {"time_utc": "TEXT DEFAULT ''", "time_epoch": "INTEGER DEFAULT 0", "type": "TEXT DEFAULT 'info'", "message": "TEXT DEFAULT ''", "details": "TEXT DEFAULT ''"},
    "deaths": {"time_utc": "TEXT DEFAULT ''", "time_epoch": "INTEGER DEFAULT 0", "source": "TEXT DEFAULT 'bot'", "reason": "TEXT DEFAULT ''", "details": "TEXT DEFAULT ''"},
}

def _table_exists(conn, name: str) -> bool:
    r = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
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
        cur.execute("INSERT INTO meta (id, schema_version, updated_time_utc) VALUES (1, ?, ?)", (SCHEMA_VERSION, utc_now_iso()))
    else:
        cur.execute("UPDATE meta SET schema_version=?, updated_time_utc=? WHERE id=1", (SCHEMA_VERSION, utc_now_iso()))

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
    cur.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})", vals)
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id

def add_event(ev_type: str, message: str, details=None):
    details = details or {}
    t = utc_now_iso()
    insert_row("events", {"time_utc": t, "time_epoch": _to_epoch(t), "type": ev_type, "message": message, "details": json.dumps(details)})

# ==========================================================
# VAULT (Simple PIN + encrypted keys, no passkeys)
# ==========================================================
VAULT_SESSION_TTL_SEC = int(os.getenv("VAULT_SESSION_TTL_SEC", "300"))

def _vault_key_bytes() -> Optional[bytes]:
    b64 = (os.getenv("VAULT_MASTER_KEY") or "").strip()
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
        if len(raw) != 32:
            return None
        return raw
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
        "notes": [
            "No withdrawal endpoints exist in this API.",
            "Vault keys are encrypted-at-rest using VAULT_MASTER_KEY.",
            "Unlock is time-limited (session TTL).",
        ],
        "ttl_sec": int(VAULT_SESSION_TTL_SEC),
    })

# ==========================================================
# Paper Trading Engine
# ==========================================================

@dataclass
class PaperPosition:
    market: str
    side: str
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


PAPER = PaperState()
PAPER_TRADES: List[Dict[str, Any]] = []


def compute_ohlc(market: str, interval_sec: int = 60, limit: int = 200):
    market = market.upper()
    conn = get_conn()
    rows = conn.execute(
        "SELECT time_epoch, price FROM prices WHERE market=? ORDER BY time_epoch DESC LIMIT 5000",
        (market,),
    ).fetchall()
    conn.close()

    if not rows:
        return []

    ticks = [{"t": r["time_epoch"], "p": r["price"]} for r in rows][::-1]
    buckets = {}

    for tick in ticks:
        b = (tick["t"] // interval_sec) * interval_sec
        if b not in buckets:
            buckets[b] = {"t": b, "o": tick["p"], "h": tick["p"], "l": tick["p"], "c": tick["p"]}
        else:
            d = buckets[b]
            d["h"] = max(d["h"], tick["p"])
            d["l"] = min(d["l"], tick["p"])
            d["c"] = tick["p"]

    return list(buckets.values())[-limit:]


@app.get("/paper/state")
def paper_state():
    return jsonify({
        "cash_usd": PAPER.cash_usd,
        "equity_usd": PAPER.equity_usd,
        "drawdown_pct": PAPER.drawdown_pct,
        "position": asdict(PAPER.position) if PAPER.position else None,
        "trades": len(PAPER_TRADES),
    })


# ==========================================================
# Basic routes + ingest
# ==========================================================

@app.get("/health")
def health():
    return jsonify({"ok": True, "time": utc_now_iso()})


@app.post("/ingest/prices")
def ingest_prices():
    body = request.get_json(force=True) or {}
    now = utc_now_iso()
    epoch = _to_epoch(now)

    conn = get_conn()
    try:
        for m, p in body.items():
            conn.execute(
                "INSERT INTO prices(time_utc,time_epoch,market,price) VALUES(?,?,?,?)",
                (now, epoch, m.upper(), float(p)),
            )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"ok": True})


@app.get("/ohlc")
def ohlc():
    market = request.args.get("market", "BTCUSDT")
    candles = compute_ohlc(market)
    return jsonify(candles)


@app.get("/")
def home():
    return jsonify({
        "ok": True,
        "service": "crypto-ai-api",
        "vault_enabled": vault_enabled(),
        "vault_unlocked": vault_unlocked(),
    })


# ==========================================================
# Main
# ==========================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

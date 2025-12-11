import os
import csv
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, send_from_directory, request, abort


# =========================================================
# CONFIG
# =========================================================

DATA_DIR = os.getenv("DATA_DIR", "/opt/render/project/src/data").rstrip("/")
TRADES_FILE = os.path.join(DATA_DIR, "trades.csv")
EQUITY_FILE = os.path.join(DATA_DIR, "equity_curve.csv")
HEARTBEAT_FILE = os.path.join(DATA_DIR, "heartbeat.json")
TRAINING_FILE = os.path.join(DATA_DIR, "training_events.csv")

# Optional simple auth for ingestion endpoints
INGEST_TOKEN = os.getenv("INGEST_TOKEN", "")

# Flask app serves dashboard from /static/index.html (NO JINJA templates)
app = Flask(__name__, static_folder="static")


# =========================================================
# HELPERS
# =========================================================

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _to_float(x: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default

def _read_csv(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append(dict(r))
    except Exception:
        return []
    return rows

def _append_csv(path: str, fieldnames: List[str], row: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            w.writeheader()
        w.writerow(row)

def _read_json(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _require_ingest_token():
    if not INGEST_TOKEN:
        return
    token = request.headers.get("X-INGEST-TOKEN", "")
    if token != INGEST_TOKEN:
        abort(401)

def _safe_round(x: Optional[float], n: int = 2) -> float:
    try:
        return round(float(x), n)
    except Exception:
        return 0.0


# =========================================================
# ROUTES (DASHBOARD)
# =========================================================

@app.get("/")
def home():
    """
    Serve the dashboard page from static/index.html.
    This avoids TemplateNotFound errors completely.
    """
    return send_from_directory(app.static_folder, "index.html")

@app.get("/raw")
def raw_dashboard_file():
    """
    Handy debug: fetch the index.html directly.
    """
    return send_from_directory(app.static_folder, "index.html")

@app.get("/data")
def data():
    """
    Dashboard JSON: reads from Render Disk CSV/JSON.
    """
    trades_rows = _read_csv(TRADES_FILE)
    equity_rows = _read_csv(EQUITY_FILE)
    training_rows = _read_csv(TRAINING_FILE)

    hb = _read_json(HEARTBEAT_FILE) or {}
    status = hb.get("status", "unknown")
    last_heartbeat = hb.get("time_utc", None)

    # Recent trades (latest 20)
    # trades.csv has: time_utc,market,side,entry_price,exit_price,qty,pnl_usd,pnl_pct,reason
    def _trade_time_key(r: Dict[str, Any]) -> str:
        return r.get("time_utc") or ""

    trades_rows_sorted = sorted(trades_rows, key=_trade_time_key, reverse=True)
    recent_trades = trades_rows_sorted[:20]

    # Win/loss + pnl
    total_trades = len(trades_rows)
    wins = 0
    losses = 0
    pnl_total = 0.0
    pnl_wins_sum = 0.0
    pnl_losses_sum = 0.0
    best_trade = 0.0
    worst_trade = 0.0

    for r in trades_rows:
        pnl = _to_float(r.get("pnl_usd"), 0.0) or 0.0
        pnl_total += pnl
        best_trade = max(best_trade, pnl)
        worst_trade = min(worst_trade, pnl)
        if pnl > 0:
            wins += 1
            pnl_wins_sum += pnl
        elif pnl < 0:
            losses += 1
            pnl_losses_sum += pnl

    win_rate = (wins / total_trades) * 100.0 if total_trades else 0.0

    # Per market performance
    per_market_map: Dict[str, Dict[str, Any]] = {}
    for r in trades_rows:
        m = (r.get("market") or "").upper()
        if not m:
            continue
        pnl = _to_float(r.get("pnl_usd"), 0.0) or 0.0
        pm = per_market_map.setdefault(m, {"market": m, "trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0})
        pm["trades"] += 1
        pm["total_pnl"] += pnl
        if pnl > 0:
            pm["wins"] += 1
        elif pnl < 0:
            pm["losses"] += 1

    per_market = []
    for m, pm in per_market_map.items():
        wr = (pm["wins"] / pm["trades"]) * 100.0 if pm["trades"] else 0.0
        per_market.append({
            "market": m,
            "trades": pm["trades"],
            "win_rate": wr,
            "total_pnl": pm["total_pnl"],
            "avg_pnl": (pm["total_pnl"] / pm["trades"]) if pm["trades"] else 0.0
        })
    per_market.sort(key=lambda x: x["total_pnl"], reverse=True)

    # Equity curve + max drawdown
    equity_curve = []
    equity_values = []

    for r in equity_rows:
        e = _to_float(r.get("equity_usd"), None)
        if e is None:
            continue
        equity_curve.append({"time_utc": r.get("time_utc"), "equity_usd": e})
        equity_values.append(e)

    max_drawdown = 0.0
    if equity_values:
        peak = equity_values[0]
        for e in equity_values:
            peak = max(peak, e)
            dd = peak - e
            max_drawdown = max(max_drawdown, dd)

    profit_factor = None
    if pnl_losses_sum < 0:
        profit_factor = pnl_wins_sum / abs(pnl_losses_sum) if abs(pnl_losses_sum) > 1e-9 else None

    avg_win = (pnl_wins_sum / wins) if wins else 0.0
    avg_loss = (pnl_losses_sum / losses) if losses else 0.0  # negative

    payload = {
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl_usd": _safe_round(pnl_total, 2),
        "bot_status": {
            "status": status,
            "last_heartbeat": last_heartbeat,
        },
        "advanced_metrics": {
            "profit_factor": profit_factor,
            "avg_win_usd": _safe_round(avg_win, 2),
            "avg_loss_usd": _safe_round(abs(avg_loss), 2),
            "best_trade_usd": _safe_round(best_trade, 2),
            "worst_trade_usd": _safe_round(worst_trade, 2),
            "max_drawdown_usd": _safe_round(max_drawdown, 2),
            "recovery_factor": None,
            "sharpe_ratio": None,
        },
        "equity_curve": equity_curve,
        "per_market": per_market,
        "recent_trades": recent_trades,
        "total_events": len(training_rows),
        "server_time_utc": utc_now_iso(),
        "data_dir": DATA_DIR,
        "files_found": {
            "trades_csv": os.path.exists(TRADES_FILE),
            "equity_csv": os.path.exists(EQUITY_FILE),
            "heartbeat_json": os.path.exists(HEARTBEAT_FILE),
            "training_csv": os.path.exists(TRAINING_FILE),
        }
    }

    return jsonify(payload)


# =========================================================
# ROUTES (INGEST) - OPTIONAL, SAFE
# =========================================================

@app.post("/ingest/heartbeat")
def ingest_heartbeat():
    _require_ingest_token()
    hb = request.get_json(silent=True) or {}
    hb.setdefault("time_utc", utc_now_iso())
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
        json.dump(hb, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True})

@app.post("/ingest/equity")
def ingest_equity():
    _require_ingest_token()
    obj = request.get_json(silent=True) or {}
    row = {
        "time_utc": obj.get("time_utc") or utc_now_iso(),
        "equity_usd": obj.get("equity_usd", 0.0),
    }
    _append_csv(EQUITY_FILE, ["time_utc", "equity_usd"], row)
    return jsonify({"ok": True})

@app.post("/ingest/trade")
def ingest_trade():
    _require_ingest_token()
    obj = request.get_json(silent=True) or {}
    row = {
        "time_utc": obj.get("time_utc") or utc_now_iso(),
        "market": obj.get("market", ""),
        "side": obj.get("side", ""),
        "entry_price": obj.get("entry_price", ""),
        "exit_price": obj.get("exit_price", ""),
        "qty": obj.get("qty", ""),
        "pnl_usd": obj.get("pnl_usd", ""),
        "pnl_pct": obj.get("pnl_pct", ""),
        "reason": obj.get("reason", ""),
    }
    _append_csv(TRADES_FILE,
                ["time_utc", "market", "side", "entry_price", "exit_price", "qty", "pnl_usd", "pnl_pct", "reason"],
                row)
    return jsonify({"ok": True})

@app.post("/ingest/training_event")
def ingest_training_event():
    _require_ingest_token()
    obj = request.get_json(silent=True) or {}
    row = {
        "time_utc": obj.get("time_utc") or utc_now_iso(),
        "event_type": obj.get("event_type", ""),
        "market": obj.get("market", ""),
        "payload_json": json.dumps(obj.get("payload_json", {}), ensure_ascii=False),
    }
    _append_csv(TRAINING_FILE, ["time_utc", "event_type", "market", "payload_json"], row)
    return jsonify({"ok": True})


# =========================================================
# STATIC FILES
# =========================================================

@app.get("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(app.static_folder, filename)


# =========================================================
# ENTRY
# =========================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

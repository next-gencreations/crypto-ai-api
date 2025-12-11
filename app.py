import os
import json
import csv
from datetime import datetime, timezone
from flask import Flask, jsonify, request, send_from_directory, abort

app = Flask(__name__, static_folder="static")

# Render disk (for THIS web service)
DATA_DIR = os.environ.get("DATA_DIR", "/opt/render/project/src/data")
os.makedirs(DATA_DIR, exist_ok=True)

TRADES_FILE = os.path.join(DATA_DIR, "trades.csv")
EQUITY_FILE = os.path.join(DATA_DIR, "equity_curve.csv")
TRAINING_FILE = os.path.join(DATA_DIR, "training_events.csv")
HEARTBEAT_FILE = os.path.join(DATA_DIR, "heartbeat.json")

INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "").strip()  # optional shared secret


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _require_token_if_set():
    if not INGEST_TOKEN:
        return
    token = request.headers.get("X-INGEST-TOKEN", "")
    if token != INGEST_TOKEN:
        abort(401)


def _safe_read_json(path: str):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _append_csv(path: str, fieldnames: list[str], row: dict):
    file_exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            w.writeheader()
        # keep only known fields
        w.writerow({k: row.get(k, "") for k in fieldnames})


def _read_csv(path: str):
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            r = csv.DictReader(f)
            for row in r:
                out.append(row)
    except Exception:
        return []
    return out


def _to_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


@app.get("/")
def home():
    # Serve dashboard HTML directly
    return send_from_directory(app.static_folder, "index.html")


@app.get("/health")
def health():
    return jsonify({"ok": True, "time_utc": _utc_now()})


# -----------------------
# Ingest endpoints (worker posts here)
# -----------------------

@app.post("/ingest/heartbeat")
def ingest_heartbeat():
    _require_token_if_set()
    payload = request.get_json(silent=True) or {}
    payload.setdefault("time_utc", _utc_now())
    payload.setdefault("status", "running")

    with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f)

    return jsonify({"ok": True})


@app.post("/ingest/trade")
def ingest_trade():
    _require_token_if_set()
    row = request.get_json(silent=True) or {}

    fields = [
        "time_utc", "market", "side",
        "entry_price", "exit_price",
        "qty", "pnl_usd", "pnl_pct",
        "reason"
    ]
    row.setdefault("time_utc", _utc_now())
    _append_csv(TRADES_FILE, fields, row)
    return jsonify({"ok": True})


@app.post("/ingest/equity")
def ingest_equity():
    _require_token_if_set()
    row = request.get_json(silent=True) or {}

    fields = ["time_utc", "equity_usd"]
    row.setdefault("time_utc", _utc_now())
    _append_csv(EQUITY_FILE, fields, row)
    return jsonify({"ok": True})


@app.post("/ingest/training_event")
def ingest_training_event():
    _require_token_if_set()
    row = request.get_json(silent=True) or {}

    # flexible schema – keep it simple
    fields = ["time_utc", "event_type", "market", "payload_json"]
    row.setdefault("time_utc", _utc_now())
    if "payload_json" not in row:
        row["payload_json"] = json.dumps(row.get("payload", {}))
    _append_csv(TRAINING_FILE, fields, row)
    return jsonify({"ok": True})


# -----------------------
# Dashboard data endpoint
# -----------------------

@app.get("/data")
def data():
    hb = _safe_read_json(HEARTBEAT_FILE) or {}
    status = hb.get("status", "unknown")
    last_heartbeat = hb.get("time_utc")

    trades = _read_csv(TRADES_FILE)
    equity_rows = _read_csv(EQUITY_FILE)

    # Compute trade metrics
    total_trades = len(trades)
    wins = 0
    losses = 0
    pnl_total = 0.0

    per_market_map = {}

    pnl_wins_sum = 0.0
    pnl_losses_sum = 0.0
    best_trade = 0.0
    worst_trade = 0.0

    recent_trades = []
    for t in trades[-20:]:
        recent_trades.append({
            "time_utc": t.get("time_utc"),
            "market": t.get("market"),
            "pnl_usd": _to_float(t.get("pnl_usd"), 0.0),
            "pnl_pct": _to_float(t.get("pnl_pct"), 0.0),
            "side": t.get("side"),
            "reason": t.get("reason"),
        })

    for t in trades:
        m = t.get("market", "UNKNOWN")
        p = _to_float(t.get("pnl_usd"), 0.0)
        pnl_total += p

        if p > 0:
            wins += 1
            pnl_wins_sum += p
        elif p < 0:
            losses += 1
            pnl_losses_sum += p  # negative

        best_trade = max(best_trade, p)
        worst_trade = min(worst_trade, p)

        pm = per_market_map.setdefault(m, {"market": m, "trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0})
        pm["trades"] += 1
        pm["total_pnl"] += p
        if p > 0:
            pm["wins"] += 1
        elif p < 0:
            pm["losses"] += 1

    win_rate = (wins / total_trades) if total_trades else 0.0

    per_market = []
    for m, pm in per_market_map.items():
        wr = (pm["wins"] / pm["trades"]) if pm["trades"] else 0.0
        per_market.append({
            "market": m,
            "trades": pm["trades"],
            "win_rate": wr,
            "total_pnl": pm["total_pnl"],
            "avg_pnl": (pm["total_pnl"] / pm["trades"]) if pm["trades"] else 0.0
        })
    per_market.sort(key=lambda x: x["total_pnl"], reverse=True)

    # Equity curve & drawdown
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
        "total_pnl_usd": pnl_total,
        "bot_status": {
            "status": status,
            "last_heartbeat": last_heartbeat,
        },
        "advanced_metrics": {
            "profit_factor": profit_factor,
            "avg_win_usd": avg_win,
            "avg_loss_usd": abs(avg_loss),
            "best_trade_usd": best_trade,
            "worst_trade_usd": worst_trade,
            "max_drawdown_usd": max_drawdown,
            "recovery_factor": None,
            "sharpe_ratio": None,
        },
        "equity_curve": equity_curve,
        "per_market": per_market,
        "recent_trades": recent_trades,
        "total_events": len(_read_csv(TRAINING_FILE)),
    }

    return jsonify(payload)


@app.get("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(app.static_folder, filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

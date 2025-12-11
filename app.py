from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, send_from_directory

app = Flask(__name__, static_folder="static")

DATA_DIR = Path(os.getenv("DATA_DIR", "/opt/render/project/src/data"))

TRADES_FILE = DATA_DIR / "trades.csv"
TRAINING_FILE = DATA_DIR / "training_events.csv"
EQUITY_FILE = DATA_DIR / "equity_curve.csv"
HEARTBEAT_FILE = DATA_DIR / "heartbeat.json"


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open("r", newline="") as f:
            reader = csv.DictReader(f)
            return [dict(r) for r in reader]
    except Exception:
        return []


def to_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def compute_bot_status() -> Dict[str, Any]:
    if not HEARTBEAT_FILE.exists():
        return {"status": "unknown", "last_heartbeat": None, "minutes_since": None}

    try:
        data = json.loads(HEARTBEAT_FILE.read_text())
        last_str = data.get("last_heartbeat")
        if not last_str:
            return {"status": "unknown", "last_heartbeat": None, "minutes_since": None}

        last_dt = datetime.fromisoformat(last_str)
        now = datetime.now(timezone.utc)
        minutes = (now - last_dt).total_seconds() / 60.0

        if minutes < 10:
            status = "running"
        elif minutes < 60:
            status = "idle"
        else:
            status = "stopped"

        return {
            "status": status,
            "last_heartbeat": last_str,
            "minutes_since": round(minutes, 1),
        }
    except Exception:
        return {"status": "unknown", "last_heartbeat": None, "minutes_since": None}


def compute_equity_curve() -> List[Dict[str, Any]]:
    rows = read_csv_rows(EQUITY_FILE)
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({"time": r.get("time"), "equity": to_float(r.get("equity"))})
    return out


def compute_summary(training_rows: List[Dict[str, str]]) -> Dict[str, Any]:
    pnl = [to_float(r.get("pnl_usd")) for r in training_rows]
    wins = sum(1 for x in pnl if x > 0)
    losses = sum(1 for x in pnl if x < 0)
    total = len(pnl)
    total_pnl = sum(pnl)
    win_rate = (wins / total * 100.0) if total else 0.0
    return {
        "total_events": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl_usd": total_pnl,
    }


def compute_per_market(training_rows: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    by_market: Dict[str, List[float]] = {}
    for r in training_rows:
        m = r.get("market", "UNKNOWN")
        by_market.setdefault(m, []).append(to_float(r.get("pnl_usd")))

    out: List[Dict[str, Any]] = []
    for market, pnls in by_market.items():
        trades = len(pnls)
        wins = sum(1 for x in pnls if x > 0)
        losses = sum(1 for x in pnls if x < 0)
        win_rate = (wins / trades * 100.0) if trades else 0.0
        total_pnl = sum(pnls)
        avg_pnl = (total_pnl / trades) if trades else 0.0
        out.append(
            {
                "market": market,
                "trades": trades,
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate,
                "total_pnl": total_pnl,
                "avg_pnl": avg_pnl,
            }
        )
    out.sort(key=lambda x: x["market"])
    return out


def compute_advanced(training_rows: List[Dict[str, str]]) -> Dict[str, Any]:
    pnls = [to_float(r.get("pnl_usd")) for r in training_rows]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]

    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    best_trade = max(pnls) if pnls else 0.0
    worst_trade = min(pnls) if pnls else 0.0

    gross_profit = sum(wins)
    gross_loss = sum(losses)  # negative
    profit_factor = (gross_profit / abs(gross_loss)) if gross_loss < 0 else None

    # Max drawdown from equity curve
    eq = compute_equity_curve()
    max_dd = 0.0
    if eq:
        peak = eq[0]["equity"]
        for p in eq:
            e = float(p["equity"])
            if e > peak:
                peak = e
            dd = peak - e
            if dd > max_dd:
                max_dd = dd

    recovery_factor = None
    total_pnl = sum(pnls)
    if max_dd > 0:
        recovery_factor = total_pnl / max_dd

    return {
        "avg_win_usd": avg_win,
        "avg_loss_usd": avg_loss,
        "best_trade_usd": best_trade,
        "worst_trade_usd": worst_trade,
        "max_drawdown_usd": max_dd,
        "profit_factor": profit_factor,
        "recovery_factor": recovery_factor,
        "sharpe_ratio": None,  # can add later once we have lots of trades
    }


def recent_trades(limit: int = 20) -> List[Dict[str, Any]]:
    rows = read_csv_rows(TRADES_FILE)
    rows = rows[-limit:]
    out = []
    for r in reversed(rows):
        out.append(
            {
                "entry_time": r.get("entry_time"),
                "exit_time": r.get("exit_time"),
                "market": r.get("market"),
                "pnl_usd": to_float(r.get("pnl_usd")),
            }
        )
    return out


@app.route("/")
def index():
    # Serve the dashboard html from /static/index.html (most reliable on Render)
    return send_from_directory("static", "index.html")


@app.route("/data")
def data():
    training_rows = read_csv_rows(TRAINING_FILE)
    summary = compute_summary(training_rows)

    payload = {
        "advanced_metrics": compute_advanced(training_rows),
        "bot_status": compute_bot_status(),
        "equity_curve": compute_equity_curve(),
        "per_market": compute_per_market(training_rows),
        "recent_trades": recent_trades(),
        "total_events": summary["total_events"],
        "wins": summary["wins"],
        "losses": summary["losses"],
        "win_rate": summary["win_rate"],
        "total_pnl_usd": summary["total_pnl_usd"],
    }
    return jsonify(payload)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

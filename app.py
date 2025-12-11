import os
import csv
import json
import math
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request, send_from_directory

# ---------------------------------------------------------------------
# Config / paths
# ---------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"  # IMPORTANT: mount Render disk here

TRADE_FILE = DATA_DIR / "trades.csv"
EQUITY_FILE = DATA_DIR / "equity_curve.csv"
TRAINING_FILE = DATA_DIR / "training_events.csv"
HEARTBEAT_FILE = DATA_DIR / "heartbeat.json"

app = Flask(
    __name__,
    static_folder=str(BASE_DIR / "static"),
    static_url_path="",
)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def ensure_data_files_exist() -> None:
    """
    Make sure the data directory and CSVs exist.
    This is defensive: the worker also initialises these, but the API
    should not crash if it starts first.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not TRADE_FILE.exists():
        with open(TRADE_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "entry_time",
                    "exit_time",
                    "hold_minutes",
                    "market",
                    "entry_price",
                    "exit_price",
                    "qty",
                    "pnl_usd",
                    "pnl_pct",
                    "take_profit_pct",
                    "stop_loss_pct",
                    "risk_mode",
                    "trend_strength",
                    "rsi",
                    "volatility",
                ]
            )

    if not EQUITY_FILE.exists():
        with open(EQUITY_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["time", "equity_usd"])

    if not TRAINING_FILE.exists():
        with open(TRAINING_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "entry_time",
                    "exit_time",
                    "hold_minutes",
                    "market",
                    "entry_price",
                    "exit_price",
                    "qty",
                    "pnl_usd",
                    "pnl_pct",
                    "take_profit_pct",
                    "stop_loss_pct",
                    "risk_mode",
                    "trend_strength",
                    "rsi",
                    "volatility",
                ]
            )


def parse_iso(ts: str) -> datetime:
    """Parse ISO timestamp safely."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def safe_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


# ---------------------------------------------------------------------
# Metrics from trades / equity
# ---------------------------------------------------------------------


def compute_equity_drawdown(equity_rows: List[Dict[str, Any]]) -> Decimal:
    """
    Compute max drawdown from equity curve.
    equity_rows: list of {"time": datetime, "equity_usd": Decimal}
    """
    if not equity_rows:
        return Decimal("0")

    peak = equity_rows[0]["equity_usd"]
    max_dd = Decimal("0")

    for row in equity_rows:
        eq = row["equity_usd"]
        if eq > peak:
            peak = eq
        drawdown = peak - eq
        if drawdown > max_dd:
            max_dd = drawdown

    return max_dd


def compute_advanced_metrics(pnls: List[Decimal], equity_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Profit factor, avg win/loss, best/worst trade, recovery factor, Sharpe-ish ratio."""
    if not pnls:
        return {
            "profit_factor": None,
            "avg_win_usd": Decimal("0"),
            "avg_loss_usd": Decimal("0"),
            "best_trade_usd": Decimal("0"),
            "worst_trade_usd": Decimal("0"),
            "max_drawdown_usd": Decimal("0"),
            "recovery_factor": None,
            "sharpe_ratio": None,
        }

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    total_win = sum(wins) if wins else Decimal("0")
    total_loss = sum(losses) if losses else Decimal("0")  # negative

    profit_factor = None
    if total_loss < 0:
        profit_factor = float(total_win / abs(total_loss)) if total_win > 0 else 0.0

    avg_win = mean(wins) if wins else Decimal("0")
    avg_loss = mean(losses) if losses else Decimal("0")
    best_trade = max(pnls)
    worst_trade = min(pnls)

    max_dd = compute_equity_drawdown(equity_rows)

    recovery_factor = None
    if max_dd > 0:
        recovery_factor = float((total_win + total_loss) / max_dd)

    # very rough Sharpe-style metric: mean / std of PnL
    sharpe_ratio = None
    if len(pnls) > 1:
        mean_pnl = sum(pnls) / Decimal(len(pnls))
        var = sum((p - mean_pnl) ** 2 for p in pnls) / Decimal(len(pnls) - 1)
        std = var.sqrt()
        if std > 0:
            sharpe_ratio = float(mean_pnl / std)

    return {
        "profit_factor": profit_factor,
        "avg_win_usd": float(avg_win),
        "avg_loss_usd": float(avg_loss),
        "best_trade_usd": float(best_trade),
        "worst_trade_usd": float(worst_trade),
        "max_drawdown_usd": float(max_dd),
        "recovery_factor": recovery_factor,
        "sharpe_ratio": sharpe_ratio,
    }


def compute_metrics_from_trades(rows: List[Dict[str, Any]], equity_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compute total trades, win rate, per-market stats, recent trades, advanced metrics.
    """
    if not rows:
        return {
            "wins": 0,
            "losses": 0,
            "total_events": 0,
            "win_rate": 0.0,
            "avg_pnl_usd": 0.0,
            "total_pnl_usd": 0.0,
            "per_market": [],
            "recent_trades": [],
            "advanced_metrics": compute_advanced_metrics([], equity_rows),
        }

    pnls = [safe_decimal(r.get("pnl_usd", "0")) for r in rows]
    wins_list = [p for p in pnls if p > 0]
    losses_list = [p for p in pnls if p < 0]

    wins = len(wins_list)
    losses = len(losses_list)
    total = wins + losses

    total_pnl = sum(pnls)
    avg_pnl = total_pnl / Decimal(total) if total > 0 else Decimal("0")

    win_rate = float(wins * 100.0 / total) if total > 0 else 0.0

    # per-market aggregation
    per_market: Dict[str, Dict[str, Any]] = {}
    for r, pnl in zip(rows, pnls):
        market = r.get("market", "UNKNOWN")
        stats = per_market.setdefault(
            market,
            {"market": market, "trades": 0, "wins": 0, "losses": 0, "pnl_list": []},
        )
        stats["trades"] += 1
        stats["pnl_list"].append(pnl)
        if pnl > 0:
            stats["wins"] += 1
        elif pnl < 0:
            stats["losses"] += 1

    per_market_list: List[Dict[str, Any]] = []
    for m, stats in per_market.items():
        trades = stats["trades"]
        wins_m = stats["wins"]
        pnl_list = stats["pnl_list"]
        total_pnl_m = sum(pnl_list)
        avg_pnl_m = total_pnl_m / Decimal(trades) if trades > 0 else Decimal("0")
        win_rate_m = float(wins_m * 100.0 / trades) if trades > 0 else 0.0

        per_market_list.append(
            {
                "market": m,
                "trades": trades,
                "wins": wins_m,
                "losses": stats["losses"],
                "win_rate": win_rate_m,
                "total_pnl_usd": float(total_pnl_m),
                "avg_pnl_usd": float(avg_pnl_m),
            }
        )

    # sort by total PnL descending for nicer display
    per_market_list.sort(key=lambda x: x["total_pnl_usd"], reverse=True)

    # recent trades (last 20 by exit_time)
    def row_exit_time(r: Dict[str, Any]) -> datetime:
        try:
            return parse_iso(r["exit_time"])
        except Exception:
            return datetime.fromtimestamp(0, tz=timezone.utc)

    sorted_rows = sorted(rows, key=row_exit_time)
    recent_rows = sorted_rows[-20:]

    recent_trades = [
        {
            "time": r.get("exit_time"),
            "market": r.get("market"),
            "pnl_usd": float(safe_decimal(r.get("pnl_usd", "0"))),
        }
        for r in recent_rows
    ]

    advanced = compute_advanced_metrics(pnls, equity_rows)

    return {
        "wins": wins,
        "losses": losses,
        "total_events": total,
        "win_rate": win_rate,
        "avg_pnl_usd": float(avg_pnl),
        "total_pnl_usd": float(total_pnl),
        "per_market": per_market_list,
        "recent_trades": recent_trades,
        "advanced_metrics": advanced,
    }


def get_bot_status() -> Dict[str, Any]:
    """
    Read heartbeat.json written by the worker and classify
    running / idle / stopped / unknown.
    """
    try:
        if not HEARTBEAT_FILE.exists():
            return {"status": "unknown", "last_heartbeat": None, "minutes_since": None}

        with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        hb_raw = data.get("heartbeat_time")
        if not hb_raw:
            return {"status": "unknown", "last_heartbeat": None, "minutes_since": None}

        hb = parse_iso(hb_raw)
        now = datetime.now(timezone.utc)
        minutes = (now - hb).total_seconds() / 60.0

        if minutes <= 10:
            status = "running"
        elif minutes <= 60:
            status = "idle"
        else:
            status = "stopped"

        return {
            "status": status,
            "last_heartbeat": hb.isoformat(),
            "minutes_since": minutes,
        }
    except Exception:
        return {"status": "unknown", "last_heartbeat": None, "minutes_since": None}


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------


@app.route("/")
def index() -> Any:
    """Serve the dashboard HTML."""
    return send_from_directory(app.static_folder, "index.html")


@app.route("/health")
def health() -> Any:
    return jsonify({"status": "ok"})


@app.route("/api/training-event", methods=["POST"])
def receive_training_event() -> Any:
    """
    Called by the worker each time a trade closes.
    We append JSON to training_events.csv (for your future AI training).
    """
    ensure_data_files_exist()

    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON body provided"}), 400

    # Make sure all expected keys exist (fill with empty strings)
    keys = [
        "entry_time",
        "exit_time",
        "hold_minutes",
        "market",
        "entry_price",
        "exit_price",
        "qty",
        "pnl_usd",
        "pnl_pct",
        "take_profit_pct",
        "stop_loss_pct",
        "risk_mode",
        "trend_strength",
        "rsi",
        "volatility",
    ]
    row = [str(data.get(k, "")) for k in keys]

    try:
        # Append to training CSV
        with open(TRAINING_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(row)

        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"error": f"Failed to write training event: {e}"}), 500


@app.route("/data")
def get_data() -> Any:
    """
    Main data endpoint for the dashboard.
    Reads trades.csv, equity_curve.csv, heartbeat.json
    and returns aggregated metrics + series.
    """
    ensure_data_files_exist()

    # --- Load equity curve ---
    equity_rows: List[Dict[str, Any]] = []
    if EQUITY_FILE.exists():
        with open(EQUITY_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    equity_rows.append(
                        {
                            "time": parse_iso(row["time"]),
                            "equity_usd": safe_decimal(row["equity_usd"]),
                        }
                    )
                except Exception:
                    continue

    # JSON friendly version of last N points
    equity_json = [
        {"time": r["time"].isoformat(), "equity_usd": float(r["equity_usd"])}
        for r in equity_rows[-100:]  # keep it light
    ]

    # --- Load trades ---
    trade_rows: List[Dict[str, Any]] = []
    if TRADE_FILE.exists():
        with open(TRADE_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trade_rows.append(row)

    metrics = compute_metrics_from_trades(trade_rows, equity_rows)
    bot_status = get_bot_status()

    payload = {
        "wins": metrics["wins"],
        "losses": metrics["losses"],
        "total_events": metrics["total_events"],
        "win_rate": metrics["win_rate"],
        "avg_pnl_usd": metrics["avg_pnl_usd"],
        "total_pnl_usd": metrics["total_pnl_usd"],
        "per_market": metrics["per_market"],
        "recent_trades": metrics["recent_trades"],
        "equity_curve": equity_json,
        "bot_status": bot_status,
        "advanced_metrics": metrics["advanced_metrics"],
    }
    return jsonify(payload)


# For debugging raw CSV, if you ever want it:
@app.route("/raw/trades")
def raw_trades() -> Any:
    if not TRADE_FILE.exists():
        return jsonify([])
    with open(TRADE_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return jsonify(list(reader))


@app.route("/raw/equity")
def raw_equity() -> Any:
    if not EQUITY_FILE.exists():
        return jsonify([])
    with open(EQUITY_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return jsonify(list(reader))


if __name__ == "__main__":
    # Local dev only – on Render use gunicorn: `gunicorn app:app`
    ensure_data_files_exist()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)

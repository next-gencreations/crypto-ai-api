from flask import Flask, jsonify, send_from_directory, request
import csv
import os
import math
import statistics
from datetime import datetime, timezone
import requests

app = Flask(__name__, static_folder="static")

# Files written by the bot worker
TRADE_FILE = "data/trades.csv"
EQUITY_FILE = "data/equity_curve.csv"
HEARTBEAT_FILE = "data/bot_heartbeat.txt"


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------


def parse_iso(ts: str):
    """Parse ISO8601 string to aware UTC datetime."""
    if not ts:
        return None
    try:
        # handle ...+00:00 and ...Z forms
        if ts.endswith("Z"):
            ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts).astimezone(timezone.utc)
    except Exception:
        return None


def compute_metrics_from_trades(rows):
    """
    rows: list of dicts from trades.csv
    Returns a dict with stats & advanced metrics.
    """
    total = len(rows)
    wins = 0
    losses = 0
    pnl_list = []
    durations = []
    per_market = {}

    # For streaks
    best_win_streak = 0
    best_loss_streak = 0
    current_streak_len = 0
    current_streak_type = None  # "win" / "loss" / None

    # Sort by exit_time for streaks + recent trades
    def row_exit_time(r):
        return parse_iso(r.get("exit_time") or r.get("entry_time") or "")

    rows_sorted = sorted(rows, key=row_exit_time)

    for r in rows_sorted:
        pnl = float(r.get("pnl_usd", 0.0))
        pnl_list.append(pnl)

        dur = r.get("hold_minutes")
        if dur is not None and dur != "":
            try:
                durations.append(float(dur))
            except Exception:
                pass

        market = r.get("market", "UNKNOWN")

        if pnl > 0:
            wins += 1
            if current_streak_type == "win":
                current_streak_len += 1
            else:
                current_streak_type = "win"
                current_streak_len = 1
            best_win_streak = max(best_win_streak, current_streak_len)
        elif pnl < 0:
            losses += 1
            if current_streak_type == "loss":
                current_streak_len += 1
            else:
                current_streak_type = "loss"
                current_streak_len = 1
            best_loss_streak = max(best_loss_streak, current_streak_len)
        else:
            current_streak_type = None
            current_streak_len = 0

        pm = per_market.setdefault(
            market, {"market": market, "trades": 0, "wins": 0, "pnl": 0.0}
        )
        pm["trades"] += 1
        if pnl > 0:
            pm["wins"] += 1
        pm["pnl"] += pnl

    total_pnl = sum(pnl_list) if pnl_list else 0.0
    win_rate = (wins / total * 100.0) if total > 0 else 0.0
    avg_pnl = (total_pnl / total) if total > 0 else 0.0
    avg_dur = (sum(durations) / len(durations)) if durations else None

    # Sharpe ratio (per trade) based on PnL list
    sharpe = None
    if len(pnl_list) > 1:
        mean = statistics.mean(pnl_list)
        stdev = statistics.stdev(pnl_list)
        if stdev > 0:
            sharpe = (mean / stdev) * math.sqrt(len(pnl_list))

    # Current streak info
    if current_streak_type is None or current_streak_len == 0:
        current_streak = {"type": "none", "length": 0}
    else:
        current_streak = {"type": current_streak_type, "length": current_streak_len}

    # Per-market stats list
    per_market_list = []
    best_market = None
    worst_market = None

    for m, pm in per_market.items():
        trades = pm["trades"]
        w = pm["wins"]
        pnl = pm["pnl"]
        wr = (w / trades * 100.0) if trades > 0 else 0.0
        avg_pm = pnl / trades if trades > 0 else 0.0
        per_market_list.append(
            {
                "market": m,
                "trades": trades,
                "win_rate": wr,
                "total_pnl_usd": pnl,
                "avg_pnl_usd": avg_pm,
            }
        )
        if (best_market is None) or (pnl > best_market["total_pnl_usd"]):
            best_market = {
                "market": m,
                "total_pnl_usd": pnl,
                "win_rate": wr,
                "trades": trades,
            }
        if (worst_market is None) or (pnl < worst_market["total_pnl_usd"]):
            worst_market = {
                "market": m,
                "total_pnl_usd": pnl,
                "win_rate": wr,
                "trades": trades,
            }

    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "total_pnl_usd": total_pnl,
        "win_rate": win_rate,
        "avg_pnl_usd": avg_pnl,
        "avg_trade_minutes": avg_dur,
        "sharpe_ratio": sharpe,
        "best_market": best_market,
        "worst_market": worst_market,
        "per_market": per_market_list,
        "current_streak": current_streak,
        "best_win_streak": best_win_streak,
        "best_loss_streak": best_loss_streak,
        "rows_sorted": rows_sorted,
    }


def compute_equity_and_drawdown():
    equity_curve = []
    if not os.path.exists(EQUITY_FILE):
        return equity_curve, None

    with open(EQUITY_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                t = parse_iso(row.get("time", ""))
                eq = float(row.get("equity_usd", 0.0))
                if t:
                    equity_curve.append({"time": t, "equity": eq})
            except Exception:
                continue

    equity_curve = sorted(equity_curve, key=lambda x: x["time"])

    max_dd = None
    if equity_curve:
        peak = equity_curve[0]["equity"]
        max_dd_val = 0.0
        for point in equity_curve:
            eq = point["equity"]
            if eq > peak:
                peak = eq
            drawdown = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
            if drawdown > max_dd_val:
                max_dd_val = drawdown
        max_dd = max_dd_val

    return equity_curve, max_dd


def get_bot_status():
    if not os.path.exists(HEARTBEAT_FILE):
        return {"status": "unknown"}

    try:
        with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
            ts = f.read().strip()
        hb = parse_iso(ts)
        if not hb:
            return {"status": "unknown"}

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
        return {"status": "unknown"}


# ---------------------------------------------------------
# Routes
# ---------------------------------------------------------


@app.route("/")
def index():
    # Serve the static dashboard HTML
    return send_from_directory("static", "index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/data")
def get_data():
    # --- Trades / advanced metrics ---
    trade_rows = []
    if os.path.exists(TRADE_FILE):
        with open(TRADE_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trade_rows.append(row)

    metrics = compute_metrics_from_trades(trade_rows)
    equity_curve, max_dd = compute_equity_and_drawdown()
    bot_status = get_bot_status()

    # Prepare JSON-friendly versions
    equity_json = [
        {"time": p["time"].isoformat(), "equity": p["equity"]} for p in equity_curve
    ]

    recent_trades = []
    for r in metrics["rows_sorted"][-20:]:  # last 20 trades
        recent_trades.append(
            {
                "

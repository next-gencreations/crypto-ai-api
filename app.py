from flask import Flask, jsonify, send_from_directory
import csv
import os
from datetime import datetime, timezone
import math

app = Flask(__name__, static_folder="static")

# -------------------------------------------------------------------
# File locations (adjust names if your bot uses different ones)
# -------------------------------------------------------------------
DATA_DIR = "data"

# Main trades CSV written by your bot
TRADE_FILE = os.path.join(DATA_DIR, "trades.csv")          # <-- change to training_events.csv if needed

# Optional heartbeat file written periodically by your bot
HEARTBEAT_FILE = os.path.join(DATA_DIR, "heartbeat.txt")


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def parse_iso(ts: str):
    """Parse ISO timestamp safely."""
    if not ts:
        return None
    try:
        # Handle plain ISO or ISO with offset
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def compute_equity_and_drawdown(rows, starting_equity: float = 1000.0):
    """
    Build equity curve from per-trade PnL and compute max drawdown.
    Equity is not stored in CSV; we derive it by cumulative PnL.
    """
    equity = starting_equity
    peak = starting_equity
    max_dd = 0.0
    curve = []

    for row in rows:
        pnl = float(row.get("pnl_usd", 0) or 0)
        t_raw = row.get("time") or row.get("timestamp")
        t = parse_iso(t_raw)

        equity += pnl
        if equity > peak:
            peak = equity

        drawdown = equity - peak  # <= 0
        if drawdown < max_dd:
            max_dd = drawdown

        curve.append(
            {
                "time": (t or datetime.now(timezone.utc)).isoformat(),
                "equity_usd": round(equity, 2),
            }
        )

    return curve, round(max_dd, 2)


def compute_advanced_metrics(pnl_values, total_pnl, wins, losses):
    """Profit factor, avg win/loss, best/worst, Sharpe, recovery factor, max loss."""
    if not pnl_values:
        return {
            "profit_factor": None,
            "avg_win_usd": 0.0,
            "avg_loss_usd": 0.0,
            "best_trade_usd": 0.0,
            "worst_trade_usd": 0.0,
            "sharpe_ratio": None,
            "recovery_factor": None,
        }

    wins_list = [p for p in pnl_values if p > 0]
    losses_list = [p for p in pnl_values if p < 0]

    avg_win = sum(wins_list) / len(wins_list) if wins_list else 0.0
    avg_loss = sum(losses_list) / len(losses_list) if losses_list else 0.0
    best_trade = max(pnl_values)
    worst_trade = min(pnl_values)

    # Profit factor = gross profit / gross loss
    gross_profit = sum(wins_list)
    gross_loss = sum(losses_list)  # negative
    profit_factor = None
    if gross_loss < 0:
        profit_factor = gross_profit / abs(gross_loss)

    # Per-trade Sharpe: mean / std * sqrt(N)
    mean_pnl = total_pnl / len(pnl_values)
    variance = sum((p - mean_pnl) ** 2 for p in pnl_values) / len(pnl_values)
    std = math.sqrt(variance)
    sharpe = None
    if std > 0:
        sharpe = (mean_pnl / std) * math.sqrt(len(pnl_values))

    # Recovery factor = total net profit / |max drawdown|
    # (actual max drawdown is computed separately & injected later)
    return {
        "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
        "avg_win_usd": round(avg_win, 2),
        "avg_loss_usd": round(avg_loss, 2),
        "best_trade_usd": round(best_trade, 2),
        "worst_trade_usd": round(worst_trade, 2),
        "sharpe_ratio": round(sharpe, 2) if sharpe is not None else None,
        # recovery_factor will be filled in get_data once we know max drawdown
        "recovery_factor": None,
    }


def compute_metrics_from_trades(rows):
    """
    Core metrics from the trades CSV.
    Expect columns: time, market, pnl_usd (others are ignored if present).
    """
    total_events = len(rows)
    wins = 0
    losses = 0
    total_pnl = 0.0
    pnl_values = []
    per_market = {}

    for row in rows:
        pnl = float(row.get("pnl_usd", 0) or 0)
        market = (row.get("market") or "UNKNOWN").upper()

        total_pnl += pnl
        pnl_values.append(pnl)

        m = per_market.setdefault(
            market,
            {
                "market": market,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl_usd": 0.0,
            },
        )
        m["trades"] += 1
        m["total_pnl_usd"] += pnl

        if pnl > 0:
            wins += 1
            m["wins"] += 1
        elif pnl < 0:
            losses += 1
            m["losses"] += 1

    win_rate = (wins / total_events * 100.0) if total_events else 0.0
    avg_pnl = (total_pnl / total_events) if total_events else 0.0

    # Per-market derived fields
    per_market_list = []
    for m in per_market.values():
        t = m["trades"]
        m["win_rate"] = (m["wins"] / t * 100.0) if t else 0.0
        m["avg_pnl_usd"] = (m["total_pnl_usd"] / t) if t else 0.0
        per_market_list.append(m)

    per_market_list.sort(key=lambda mm: mm["total_pnl_usd"], reverse=True)

    advanced = compute_advanced_metrics(pnl_values, total_pnl, wins, losses)

    return {
        "total_events": total_events,
        "wins": wins,
        "losses": losses,
        "total_pnl_usd": round(total_pnl, 2),
        "win_rate": round(win_rate, 1),
        "avg_pnl_usd": round(avg_pnl, 2),
        "per_market": per_market_list,
        "advanced_metrics": advanced,
    }


def get_bot_status():
    """
    Read heartbeat file written by your trading bot.
    File should contain an ISO timestamp of the last heartbeat.
    """
    if not os.path.exists(HEARTBEAT_FILE):
        return {
            "status": "unknown",
            "last_heartbeat": None,
            "minutes_since": None,
        }

    try:
        with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except Exception:
        return {
            "status": "unknown",
            "last_heartbeat": None,
            "minutes_since": None,
        }

    hb = parse_iso(raw)
    if not hb:
        return {
            "status": "unknown",
            "last_heartbeat": raw,
            "minutes_since": None,
        }

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
        "minutes_since": round(minutes, 1),
    }


# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------
@app.route("/")
def index():
    # Serve the static dashboard HTML
    return send_from_directory("static", "index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/data")
def get_data():
    # ---- Trades + metrics ----
    trade_rows = []

    if os.path.exists(TRADE_FILE):
        with open(TRADE_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trade_rows.append(row)

    metrics = compute_metrics_from_trades(trade_rows)
    equity_curve, max_dd = compute_equity_and_drawdown(trade_rows)

    # Fill recovery factor now that we know max drawdown
    adv = metrics["advanced_metrics"]
    if max_dd < 0:
        adv["recovery_factor"] = round(
            metrics["total_pnl_usd"] / abs(max_dd), 2
        )
    else:
        adv["recovery_factor"] = None

    # JSON-friendly equity curve (limit to last 120 pts)
    MAX_POINTS = 120
    equity_json = equity_curve[-MAX_POINTS:]

    # Recent trades (last 20, newest first)
    recent_trades = []
    for row in trade_rows[-20:]:
        t_raw = row.get("time") or row.get("timestamp")
        t = parse_iso(t_raw)
        recent_trades.append(
            {
                "time": (t or datetime.now(timezone.utc)).isoformat(),
                "market": (row.get("market") or "UNKNOWN").upper(),
                "pnl_usd": round(float(row.get("pnl_usd", 0) or 0), 2),
            }
        )
    recent_trades.reverse()

    bot_status = get_bot_status()

    payload = {
        "total_events": metrics["total_events"],
        "wins": metrics["wins"],
        "losses": metrics["losses"],
        "total_pnl_usd": metrics["total_pnl_usd"],
        "win_rate": metrics["win_rate"],
        "avg_pnl_usd": metrics["avg_pnl_usd"],
        "per_market": metrics["per_market"],
        "advanced_metrics": adv,
        "equity_curve": equity_json,
        "max_drawdown_usd": max_dd,
        "recent_trades": recent_trades,
        "bot_status": bot_status,
    }

    return jsonify(payload)


# -------------------------------------------------------------------
# Entry point
# -------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

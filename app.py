from __future__ import annotations

from flask import Flask, jsonify, send_from_directory
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
import csv
import os

# High precision for PnL
getcontext().prec = 28

# -----------------------------------------------------------------------------
# Flask setup
# -----------------------------------------------------------------------------
app = Flask(__name__, static_folder="static")

DATA_DIR = Path("data")
TRADES_FILE = DATA_DIR / "trades.csv"
EQUITY_FILE = DATA_DIR / "equity_curve.csv"
TRAINING_FILE = DATA_DIR / "training_events.csv"  # kept for future AI use


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _safe_decimal(value: str | float | int, default: str = "0") -> Decimal:
    """Convert to Decimal safely."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal(default)


def load_trades():
    """
    Load all trades from data/trades.csv

    Expected columns:
    entry_time, exit_time, hold_minutes, market, entry_price, exit_price,
    qty, pnl_usd, pnl_pct, take_profit_pct, stop_loss_pct, risk_mode
    """
    if not TRADES_FILE.exists():
        return []

    trades = []
    with TRADES_FILE.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append(row)
    return trades


def load_equity_curve():
    """
    Load equity curve from data/equity_curve.csv

    Columns: time, equity_usd
    """
    if not EQUITY_FILE.exists():
        return []

    points = []
    with EQUITY_FILE.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            points.append(
                {
                    "time": row.get("time", ""),
                    "equity_usd": float(_safe_decimal(row.get("equity_usd", "0"))),
                }
            )
    return points


def compute_trade_stats(trades):
    """
    Compute:
      - global summary
      - per-market stats
    """
    total_trades = len(trades)
    wins = 0
    losses = 0
    total_pnl = Decimal("0")

    by_market = {}

    for row in trades:
        market = row.get("market", "UNKNOWN")

        pnl = _safe_decimal(row.get("pnl_usd", "0"))
        total_pnl += pnl

        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

        # Per-market bucket
        m_stats = by_market.setdefault(
            market,
            {
                "market": market,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl_usd": Decimal("0"),
            },
        )
        m_stats["trades"] += 1
        m_stats["total_pnl_usd"] += pnl
        if pnl > 0:
            m_stats["wins"] += 1
        elif pnl < 0:
            m_stats["losses"] += 1

    # Calculate derived stats
    avg_pnl = total_pnl / total_trades if total_trades > 0 else Decimal("0")
    win_rate = wins / total_trades if total_trades > 0 else 0.0

    for m_stats in by_market.values():
        t = m_stats["trades"]
        w = m_stats["wins"]
        m_stats["avg_pnl_usd"] = float(
            m_stats["total_pnl_usd"] / t if t > 0 else Decimal("0")
        )
        m_stats["total_pnl_usd"] = float(m_stats["total_pnl_usd"])
        m_stats["win_rate"] = float(w / t) if t > 0 else 0.0

    summary = {
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": float(win_rate),
        "total_pnl_usd": float(total_pnl),
        "avg_pnl_usd": float(avg_pnl),
    }

    return summary, list(by_market.values())


def build_recent_trades(trades, limit: int = 25):
    """Return most recent trades (by exit_time desc)."""
    # Sort by exit_time if present, otherwise entry_time
    sorted_trades = sorted(
        trades,
        key=lambda r: r.get("exit_time") or r.get("entry_time") or "",
        reverse=True,
    )
    recent = []
    for row in sorted_trades[:limit]:
        recent.append(
            {
                "time": row.get("exit_time") or row.get("entry_time", ""),
                "market": row.get("market", ""),
                "pnl_usd": float(_safe_decimal(row.get("pnl_usd", "0"))),
            }
        )
    return recent


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.route("/")
def index():
    """
    Serve the dashboard HTML (static/index.html).
    """
    return send_from_directory(app.static_folder, "index.html")


@app.route("/data")
def data():
    """
    Main stats endpoint used by the dashboard.
    Returns:
      - summary totals
      - per-market stats
      - equity curve
      - recent trades
    """
    trades = load_trades()
    summary, by_market = compute_trade_stats(trades)
    equity_curve = load_equity_curve()
    recent_trades = build_recent_trades(trades)

    payload = {
        **summary,
        "by_market": by_market,
        "equity_curve": equity_curve,
        "recent_trades": recent_trades,
    }
    return jsonify(payload), 200


@app.route("/health")
def health():
    """
    Simple status endpoint for 'API status' link on the dashboard.
    """
    exists_trades = TRADES_FILE.exists()
    exists_equity = EQUITY_FILE.exists()
    exists_training = TRAINING_FILE.exists()

    return jsonify(
        {
            "status": "ok",
            "trades_csv_exists": exists_trades,
            "equity_curve_csv_exists": exists_equity,
            "training_events_csv_exists": exists_training,
        }
    )


# Required for Render
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

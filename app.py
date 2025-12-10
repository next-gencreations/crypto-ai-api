from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import requests
from flask import Flask, jsonify, request, send_from_directory

# -----------------------------------------------------------------------------
# App & file paths
# -----------------------------------------------------------------------------

app = Flask(__name__, static_folder="static")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

# Where we store trades coming from the bot
TRADE_FILE = os.path.join(DATA_DIR, "trades.csv")

# Optional: future use if you ever want a separate equity log
EQUITY_FILE = os.path.join(DATA_DIR, "equity_curve.csv")

# Used so the dashboard can show bot "status" (running / idle / stopped)
HEARTBEAT_FILE = os.path.join(DATA_DIR, "heartbeat.txt")

# Starting equity for equity-curve reconstruction
START_EQUITY_USD = 1000.0


# Make sure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def parse_iso(ts: str) -> datetime:
    """
    Parse an ISO timestamp into a timezone-aware datetime.
    """
    if not ts:
        return datetime.now(timezone.utc)

    try:
        # Python 3.11+ has fromisoformat with offset support
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def row_exit_time(row: Dict[str, Any]) -> datetime:
    """
    Helper to sort rows by exit_time (fallback to entry_time).
    """
    ts = row.get("exit_time") or row.get("entry_time") or ""
    return parse_iso(ts)


def compute_equity_and_drawdown(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], float]:
    """
    Rebuild equity curve from trade PnL, starting from START_EQUITY_USD.
    Returns (equity_curve, max_drawdown).
    """
    equity = START_EQUITY_USD
    equity_curve: List[Dict[str, Any]] = []

    # sort by time
    sorted_rows = sorted(rows, key=row_exit_time)

    peak = equity
    max_dd = 0.0

    for r in sorted_rows:
        pnl = float(r.get("pnl_usd", 0.0) or 0.0)
        t = row_exit_time(r)
        equity += pnl

        if equity > peak:
            peak = equity

        drawdown = peak - equity
        if drawdown > max_dd:
            max_dd = drawdown

        equity_curve.append(
            {
                "time": t.isoformat(),
                "equity_usd": round(equity, 4),
            }
        )

    return equity_curve, max_dd


def compute_metrics_from_trades(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Aggregate trades into summary stats and per-market performance.
    """
    total_events = len(rows)
    wins = 0
    losses = 0
    total_pnl = 0.0

    per_market: Dict[str, Dict[str, Any]] = {}

    sorted_rows = sorted(rows, key=row_exit_time)

    for r in sorted_rows:
        pnl = float(r.get("pnl_usd", 0.0) or 0.0)
        total_pnl += pnl

        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

        market = r.get("market") or "UNKNOWN"
        bucket = per_market.setdefault(
            market,
            {
                "market": market,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl_usd": 0.0,
            },
        )

        bucket["trades"] += 1
        bucket["total_pnl_usd"] += pnl
        if pnl > 0:
            bucket["wins"] += 1
        elif pnl < 0:
            bucket["losses"] += 1

    win_rate = (wins / total_events) if total_events else 0.0
    avg_pnl = (total_pnl / total_events) if total_events else 0.0

    per_market_list: List[Dict[str, Any]] = []
    for bucket in per_market.values():
        t = bucket["trades"] or 1
        bucket["win_rate"] = (bucket["wins"] / t) if t else 0.0
        bucket["avg_pnl_usd"] = (bucket["total_pnl_usd"] / t) if t else 0.0
        per_market_list.append(bucket)

    return {
        "rows_sorted": sorted_rows,
        "total_events": total_events,
        "wins": wins,
        "losses": losses,
        "total_pnl_usd": total_pnl,
        "win_rate": win_rate,
        "avg_pnl_usd": avg_pnl,
        "per_market": per_market_list,
    }


def get_bot_status() -> Dict[str, Any]:
    """
    Very simple status based on a heartbeat timestamp.
    The worker should update HEARTBEAT_FILE whenever it sends a trade.
    """
    try:
        if not os.path.exists(HEARTBEAT_FILE):
            return {"status": "unknown"}

        with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
            ts = f.read().strip()

        if not ts:
            return {"status": "unknown"}

        hb = parse_iso(ts)
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


# -----------------------------------------------------------------------------
# Routes: Dashboard, health, data
# -----------------------------------------------------------------------------

@app.route("/")
def index():
    # Serve the static dashboard HTML
    return send_from_directory("static", "index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/data")
def get_data():
    # --- Load trades from CSV ---
    trade_rows: List[Dict[str, Any]] = []
    if os.path.exists(TRADE_FILE):
        with open(TRADE_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trade_rows.append(row)

    metrics = compute_metrics_from_trades(trade_rows)
    equity_curve, max_dd = compute_equity_and_drawdown(trade_rows)
    bot_status = get_bot_status()

    # Prepare recent trades (last 20, newest first)
    recent_trades: List[Dict[str, Any]] = []
    for r in metrics["rows_sorted"][-20:][::-1]:
        recent_trades.append(
            {
                "time": (r.get("exit_time") or r.get("entry_time") or ""),
                "market": r.get("market", ""),
                "pnl_usd": float(r.get("pnl_usd", 0.0) or 0.0),
            }
        )

    return jsonify(
        {
            "total_events": metrics["total_events"],
            "wins": metrics["wins"],
            "losses": metrics["losses"],
            "total_pnl_usd": metrics["total_pnl_usd"],
            "win_rate": metrics["win_rate"],
            "avg_pnl_usd": metrics["avg_pnl_usd"],
            "per_market": metrics["per_market"],
            "equity_curve": equity_curve,
            "recent_trades": recent_trades,
            "bot_status": bot_status,
            "max_drawdown": max_dd,
        }
    )


# -----------------------------------------------------------------------------
# Route: Training events from the bot
# -----------------------------------------------------------------------------

@app.route("/training-event", methods=["POST"])
@app.route("/training-events", methods=["POST"])
def training_event():
    """
    Endpoint for the worker bot to POST each closed trade.

    Expects JSON with at least:
    entry_time, exit_time, hold_minutes, market, entry_price, exit_price,
    qty, pnl_usd, pnl_pct, take_profit_pct, stop_loss_pct, risk_mode
    """
    os.makedirs(DATA_DIR, exist_ok=True)

    data = request.get_json(force=True, silent=True) or {}

    fieldnames = [
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
    ]

    file_exists = os.path.exists(TRADE_FILE)

    with open(TRADE_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()

        row = {field: str(data.get(field, "")) for field in fieldnames}
        writer.writerow(row)

    # Update heartbeat so the dashboard knows the bot is alive
    with open(HEARTBEAT_FILE, "w", encoding="utf-8") as hb:
        hb.write(datetime.now(timezone.utc).isoformat())

    return jsonify({"status": "ok"})


# -----------------------------------------------------------------------------
# Routes: Candle data (Coinbase + KuCoin) for charts
# -----------------------------------------------------------------------------

def fetch_coinbase_candles(market: str = "BTC-USD", granularity: int = 300) -> List[Dict[str, Any]]:
    """
    Coinbase spot candles.

    granularity (seconds) allowed values typically:
    60, 300, 900, 3600, 21600, 86400
    """
    url = f"https://api.exchange.coinbase.com/products/{market}/candles?granularity={granularity}"
    r = requests.get(url, timeout=10)

    if r.status_code != 200:
        return []

    candles = r.json()

    formatted: List[Dict[str, Any]] = []
    for c in candles:
        # [ time, low, high, open, close, volume ]
        formatted.append(
            {
                "time": c[0],
                "low": c[1],
                "high": c[2],
                "open": c[3],
                "close": c[4],
                "volume": c[5],
            }
        )
    return formatted


def fetch_kucoin_candles(market: str = "BTC-USDT", interval: str = "5min") -> List[Dict[str, Any]]:
    """
    KuCoin K-line candles.

    interval examples:
    1min, 5min, 15min, 1hour, 4hour, 1day
    """
    url = f"https://api.kucoin.com/api/v1/market/candles?type={interval}&symbol={market}"
    r = requests.get(url, timeout=10)

    if r.status_code != 200:
        return []

    candles = r.json().get("data", [])

    formatted: List[Dict[str, Any]] = []
    for c in candles:
        # [ time(ms), open, close, high, low, volume, turnover ]
        formatted.append(
            {
                "time": int(c[0]) / 1000,
                "open": float(c[1]),
                "close": float(c[2]),
                "high": float(c[3]),
                "low": float(c[4]),
                "volume": float(c[5]),
            }
        )
    return formatted


@app.route("/candles")
def get_candles():
    """
    GET /candles?market=BTC-USD&source=coinbase
    GET /candles?market=BTC-USDT&source=kucoin
    """
    market = request.args.get("market", "BTC-USD")
    source = request.args.get("source", "coinbase").lower()

    if source == "kucoin":
        # KuCoin uses BTC-USDT style, whereas Coinbase uses BTC-USD.
        kucoin_market = market.replace("-USD", "-USDT")
        data = fetch_kucoin_candles(kucoin_market, "5min")
    else:
        data = fetch_coinbase_candles(market, 300)

    return jsonify({"market": market, "source": source, "candles": data})


# -----------------------------------------------------------------------------
# Local dev entrypoint (Render uses gunicorn, but this is handy locally)
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)

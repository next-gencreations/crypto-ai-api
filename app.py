from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
from flask import Flask, jsonify, render_template

app = Flask(__name__, static_folder="static", template_folder=".")

DATA_DIR = Path(os.getenv("DATA_DIR", "/opt/render/project/src/data"))
TRADES_FILE = DATA_DIR / "trades.csv"
TRAINING_FILE = DATA_DIR / "training_events.csv"
EQUITY_FILE = DATA_DIR / "equity_curve.csv"
HEARTBEAT_FILE = DATA_DIR / "heartbeat.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def compute_bot_status() -> Dict[str, Any]:
    if not HEARTBEAT_FILE.exists():
        return {"status": "unknown", "last_heartbeat": None, "minutes_since": None}

    try:
        with HEARTBEAT_FILE.open() as f:
            data = json.load(f)

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
    df = safe_read_csv(EQUITY_FILE)
    if df.empty:
        return []

    out: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        out.append(
            {
                "time": row["time"],
                "equity": float(row["equity"]),
            }
        )
    return out


def compute_advanced_metrics() -> Dict[str, Any]:
    df = safe_read_csv(TRAINING_FILE)
    if df.empty:
        return {
            "avg_win_usd": 0.0,
            "avg_loss_usd": 0.0,
            "best_trade_usd": 0.0,
            "worst_trade_usd": 0.0,
            "max_drawdown_usd": 0.0,
            "profit_factor": None,
            "recovery_factor": None,
            "sharpe_ratio": None,
        }

    df = df.copy()
    df["pnl_usd"] = pd.to_numeric(df["pnl_usd"], errors="coerce").fillna(0.0)

    # Basic wins / losses
    wins = df[df["pnl_usd"] > 0]
    losses = df[df["pnl_usd"] < 0]

    avg_win = wins["pnl_usd"].mean() if not wins.empty else 0.0
    avg_loss = losses["pnl_usd"].mean() if not losses.empty else 0.0
    best_trade = df["pnl_usd"].max()
    worst_trade = df["pnl_usd"].min()

    gross_profit = wins["pnl_usd"].sum()
    gross_loss = losses["pnl_usd"].sum()  # negative

    profit_factor = None
    if gross_loss < 0:
        profit_factor = float(gross_profit / abs(gross_loss)) if abs(gross_loss) > 0 else None

    # Equity curve for drawdown metrics
    eq_df = safe_read_csv(EQUITY_FILE)
    max_drawdown_usd = 0.0
    recovery_factor = None
    if not eq_df.empty:
        eq_df = eq_df.copy()
        eq_df["equity"] = pd.to_numeric(eq_df["equity"], errors="coerce").fillna(0.0)
        peak = eq_df["equity"].iloc[0]
        max_dd = 0.0
        for v in eq_df["equity"]:
            peak = max(peak, v)
            dd = peak - v
            max_dd = max(max_dd, dd)
        max_drawdown_usd = float(max_dd)
        total_pnl = df["pnl_usd"].sum()
        if max_dd > 0:
            recovery_factor = float(total_pnl / max_dd)

    # Sharpe ratio (per-trade)
    sharpe_ratio = None
    returns = df["pnl_usd"]
    if len(returns) > 1:
        mean_ret = returns.mean()
        std_ret = returns.std(ddof=1)
        if std_ret > 0:
            sharpe_ratio = float((mean_ret / std_ret) * (len(returns) ** 0.5))

    return {
        "avg_win_usd": float(avg_win if pd.notna(avg_win) else 0.0),
        "avg_loss_usd": float(avg_loss if pd.notna(avg_loss) else 0.0),
        "best_trade_usd": float(best_trade if pd.notna(best_trade) else 0.0),
        "worst_trade_usd": float(worst_trade if pd.notna(worst_trade) else 0.0),
        "max_drawdown_usd": float(max_drawdown_usd),
        "profit_factor": profit_factor,
        "recovery_factor": recovery_factor,
        "sharpe_ratio": sharpe_ratio,
    }


def compute_summary_stats() -> Dict[str, Any]:
    df = safe_read_csv(TRAINING_FILE)
    if df.empty:
        return {
            "total_events": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl_usd": 0.0,
        }

    df = df.copy()
    df["pnl_usd"] = pd.to_numeric(df["pnl_usd"], errors="coerce").fillna(0.0)

    wins = (df["pnl_usd"] > 0).sum()
    losses = (df["pnl_usd"] < 0).sum()
    total = len(df)
    total_pnl = df["pnl_usd"].sum()

    win_rate = (wins / total * 100.0) if total > 0 else 0.0

    return {
        "total_events": int(total),
        "wins": int(wins),
        "losses": int(losses),
        "win_rate": float(win_rate),
        "total_pnl_usd": float(total_pnl),
    }


def compute_per_market_stats() -> List[Dict[str, Any]]:
    df = safe_read_csv(TRAINING_FILE)
    if df.empty:
        return []

    df = df.copy()
    df["pnl_usd"] = pd.to_numeric(df["pnl_usd"], errors="coerce").fillna(0.0)

    grouped = df.groupby("market")
    out: List[Dict[str, Any]] = []
    for market, g in grouped:
        trades = len(g)
        wins = (g["pnl_usd"] > 0).sum()
        losses = (g["pnl_usd"] < 0).sum()
        win_rate = (wins / trades * 100.0) if trades > 0 else 0.0
        total_pnl = g["pnl_usd"].sum()
        avg_pnl = g["pnl_usd"].mean() if trades > 0 else 0.0
        out.append(
            {
                "market": market,
                "trades": int(trades),
                "wins": int(wins),
                "losses": int(losses),
                "win_rate": float(win_rate),
                "total_pnl": float(total_pnl),
                "avg_pnl": float(avg_pnl),
            }
        )
    out.sort(key=lambda x: x["market"])
    return out


def get_recent_trades(limit: int = 20) -> List[Dict[str, Any]]:
    df = safe_read_csv(TRADES_FILE)
    if df.empty:
        return []

    df = df.tail(limit).copy()
    df["pnl_usd"] = pd.to_numeric(df["pnl_usd"], errors="coerce").fillna(0.0)

    out: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        out.append(
            {
                "entry_time": row["entry_time"],
                "exit_time": row["exit_time"],
                "market": row["market"],
                "pnl_usd": float(row["pnl_usd"]),
            }
        )
    # Sort newest first
    out.sort(key=lambda x: x["exit_time"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    # index.html lives in the repo root
    return render_template("index.html")


@app.route("/data")
def data():
    advanced = compute_advanced_metrics()
    summary = compute_summary_stats()
    bot_status = compute_bot_status()
    equity_curve = compute_equity_curve()
    per_market = compute_per_market_stats()
    recent_trades = get_recent_trades()

    payload = {
        "advanced_metrics": advanced,
        "bot_status": bot_status,
        "equity_curve": equity_curve,
        "per_market": per_market,
        "recent_trades": recent_trades,
        "total_events": summary["total_events"],
        "wins": summary["wins"],
        "losses": summary["losses"],
        "win_rate": summary["win_rate"],
        "total_pnl_usd": summary["total_pnl_usd"],
    }
    return jsonify(payload)


# Optional endpoint if you ever want to POST events directly
@app.route("/event", methods=["POST"])
def receive_event():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

from __future__ import annotations

from flask import Flask, jsonify, request, Response
from decimal import Decimal, getcontext
from pathlib import Path
import csv
import os
import pandas as pd

app = Flask(__name__)

# High precision for PnL math
getcontext().prec = 28

# -------------------------------------------------------------------
# File paths
# -------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

TRAINING_FILE = DATA_DIR / "training_events.csv"

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------


def load_training_df() -> pd.DataFrame:
    """Load training_events.csv into a DataFrame, or empty df if missing."""
    if not TRAINING_FILE.exists():
        return pd.DataFrame()

    try:
        df = pd.read_csv(TRAINING_FILE)
    except Exception:
        # If file is corrupt or odd, treat as empty
        return pd.DataFrame()

    # Normalise expected columns
    if "pnl_usd" in df.columns:
        # Ensure Decimal/float
        df["pnl_usd"] = pd.to_numeric(df["pnl_usd"], errors="coerce").fillna(0.0)
    else:
        df["pnl_usd"] = 0.0

    if "exit_time" not in df.columns:
        # If missing, fall back to entry_time or index
        if "entry_time" in df.columns:
            df["exit_time"] = df["entry_time"]
        else:
            df["exit_time"] = df.index.astype(str)

    if "market" not in df.columns:
        df["market"] = "UNKNOWN"

    return df


def compute_stats(df: pd.DataFrame) -> dict:
    """Compute summary stats used by /data and /api/stats."""
    if df.empty:
        return {
            "total_events": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl_usd": 0.0,
            "avg_pnl_usd": 0.0,
        }

    pnl = df["pnl_usd"]

    wins = int((pnl > 0).sum())
    losses = int((pnl < 0).sum())
    total_events = int(len(df))

    total_pnl = float(pnl.sum())
    avg_pnl = float(pnl.mean()) if total_events > 0 else 0.0
    win_rate = float(wins / total_events) if total_events > 0 else 0.0

    return {
        "total_events": total_events,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl_usd": total_pnl,
        "avg_pnl_usd": avg_pnl,
    }


def training_events_to_trades(df: pd.DataFrame, limit: int = 20) -> list[dict]:
    """Convert training events into a compact 'recent trades' list."""
    if df.empty:
        return []

    df_sorted = df.sort_values("exit_time")
    df_tail = df_sorted.tail(limit)

    trades = []
    for _, row in df_tail.iterrows():
        trades.append(
            {
                "time": str(row.get("exit_time", "")),
                "market": str(row.get("market", "")),
                "pnl_usd": float(row.get("pnl_usd", 0.0)),
            }
        )
    return trades


def training_events_to_equity_curve(
    df: pd.DataFrame, starting_equity: float = 1000.0
) -> list[dict]:
    """Build equity curve from cumulative pnl_usd."""
    if df.empty:
        return []

    df_sorted = df.sort_values("exit_time").copy()
    df_sorted["pnl_usd"] = pd.to_numeric(
        df_sorted["pnl_usd"], errors="coerce"
    ).fillna(0.0)

    df_sorted["equity"] = starting_equity + df_sorted["pnl_usd"].cumsum()

    curve = []
    for _, row in df_sorted.iterrows():
        curve.append(
            {
                "time": str(row.get("exit_time", "")),
                "equity": float(row.get("equity", starting_equity)),
            }
        )
    return curve


def append_training_event(event: dict) -> None:
    """Append a training event dict as a CSV row to training_events.csv."""
    # Normalise keys to a stable order
    # These are the columns we designed for the bot; extras will be added at the end.
    preferred_order = [
        "entry_time",
        "exit_time",
        "hold_minutes",
        "market",
        "trend_strength",
        "rsi",
        "volatility",
        "entry_price",
        "exit_price",
        "pnl_usd",
        "pnl_pct",
        "take_profit_pct",
        "stop_loss_pct",
        "risk_mode",
    ]

    # Ensure all values are strings for csv.writer
    event_str = {k: ("" if v is None else str(v)) for k, v in event.items()}

    if TRAINING_FILE.exists():
        # Append with existing header
        with TRAINING_FILE.open("r", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or list(event_str.keys())
    else:
        # First write - build fieldnames
        extra_keys = [k for k in event_str.keys() if k not in preferred_order]
        fieldnames = preferred_order + extra_keys

    # Now append
    file_exists = TRAINING_FILE.exists()
    with TRAINING_FILE.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(event_str)


# -------------------------------------------------------------------
# HTML dashboard
# -------------------------------------------------------------------

INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Crypto AI Bot Dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {
      --bg: #050816;
      --card: #111827;
      --card-soft: #020617;
      --accent: #22c55e;
      --accent-soft: rgba(34,197,94,0.1);
      --text: #e5e7eb;
      --text-soft: #9ca3af;
      --danger: #f97373;
      --font: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: radial-gradient(circle at top, #1f2937 0, var(--bg) 40%);
      color: var(--text);
      font-family: var(--font);
      padding: 16px;
    }
    .page {
      max-width: 980px;
      margin: 0 auto;
    }
    header {
      margin-bottom: 18px;
    }
    h1 {
      font-size: 1.9rem;
      letter-spacing: 0.03em;
      margin-bottom: 6px;
    }
    .sub {
      font-size: 0.9rem;
      color: var(--text-soft);
    }
    .sub a {
      color: var(--accent);
      text-decoration: none;
      margin-right: 12px;
    }
    .sub a:hover { text-decoration: underline; }

    .cards {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 14px;
      margin-bottom: 24px;
    }
    .card {
      background: linear-gradient(135deg, var(--card), var(--card-soft));
      border-radius: 14px;
      padding: 14px 16px;
      box-shadow: 0 18px 35px rgba(0,0,0,0.45);
      position: relative;
      overflow: hidden;
    }
    .card::before {
      content: "";
      position: absolute;
      inset: -40%;
      background: radial-gradient(circle at top left, rgba(34,197,94,0.18), transparent 58%);
      opacity: 0.8;
      pointer-events: none;
    }
    .card h2 {
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.13em;
      color: var(--text-soft);
      margin-bottom: 10px;
    }
    .card-main {
      font-size: 1.6rem;
      font-weight: 600;
    }
    .card-sub {
      margin-top: 6px;
      font-size: 0.8rem;
      color: var(--text-soft);
    }

    .section-title {
      font-size: 1rem;
      margin-bottom: 8px;
      margin-top: 4px;
    }

    .equity-wrap {
      background: linear-gradient(135deg, rgba(15,23,42,0.98), rgba(15,23,42,0.98));
      border-radius: 16px;
      padding: 16px;
      margin-bottom: 22px;
      box-shadow: 0 18px 35px rgba(0,0,0,0.5);
      border: 1px solid rgba(148,163,184,0.12);
    }

    canvas {
      width: 100%;
      height: 220px;
      display: block;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
      margin-top: 8px;
    }
    th, td {
      padding: 8px 6px;
      text-align: left;
      border-bottom: 1px solid rgba(31,41,55,0.8);
    }
    th {
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--text-soft);
      background: rgba(15,23,42,0.85);
    }
    tbody tr:hover {
      background: rgba(15,23,42,0.7);
    }
    .pnl-pos { color: var(--accent); }
    .pnl-neg { color: var(--danger); }
    .muted { color: var(--text-soft); font-size: 0.8rem; }

    @media (max-width: 640px) {
      h1 { font-size: 1.4rem; }
      .card-main { font-size: 1.3rem; }
      canvas { height: 180px; }
    }
  </style>
</head>
<body>
  <div class="page">
    <header>
      <h1>Crypto AI Bot Dashboard</h1>
      <p class="sub">
        Paper-trading stats from Render —
        <a href="/data" target="_blank">raw stats</a>
        <a href="/api/status" target="_blank">API status</a>
      </p>
    </header>

    <section class="cards">
      <article class="card">
        <h2>Total trades</h2>
        <div class="card-main" id="totalTrades">0</div>
        <div class="card-sub" id="winLossText">0 wins / 0 losses</div>
      </article>

      <article class="card">
        <h2>Win rate</h2>
        <div class="card-main" id="winRate">0%</div>
        <div class="card-sub" id="avgPnlText">Avg +$0.00 per trade</div>
      </article>

      <article class="card">
        <h2>PNL (total)</h2>
        <div class="card-main" id="totalPnl">$0.00</div>
        <div class="card-sub muted" id="pnlNote">No equity data yet</div>
      </article>
    </section>

    <section class="equity-wrap">
      <div class="section-title">Equity Curve</div>
      <canvas id="equityCanvas"></canvas>
      <div class="muted" id="equityNote">Waiting for trades...</div>
    </section>

    <section>
      <div class="section-title">Recent Trades</div>
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Market</th>
            <th>PNL (USD)</th>
          </tr>
        </thead>
        <tbody id="tradesBody">
          <tr><td colspan="3" class="muted">Loading…</td></tr>
        </tbody>
      </table>
    </section>
  </div>

  <script>
    async function fetchJson(path) {
      const res = await fetch(path);
      if (!res.ok) {
        throw new Error("Request failed: " + path + " (" + res.status + ")");
      }
      return await res.json();
    }

    function formatUsd(value) {
      const sign = value >= 0 ? "+" : "";
      return sign + "$" + value.toFixed(2);
    }

    async function loadStats() {
      try {
        const stats = await fetchJson("/api/stats");
        const total = stats.total_events || 0;
        const wins = stats.wins || 0;
        const losses = stats.losses || 0;
        const winRate = (stats.win_rate || 0) * 100;
        const totalPnl = stats.total_pnl_usd || 0;
        const avgPnl = stats.avg_pnl_usd || 0;

        document.getElementById("totalTrades").textContent = total;
        document.getElementById("winLossText").textContent =
          wins + " wins / " + losses + " losses";
        document.getElementById("winRate").textContent = winRate.toFixed(1) + "%";
        document.getElementById("avgPnlText").textContent =
          "Avg " + formatUsd(avgPnl) + " per trade";
        document.getElementById("totalPnl").textContent = formatUsd(totalPnl);
        document.getElementById("pnlNote").textContent =
          total > 0 ? "Based on closed trades only" : "No equity data yet";
      } catch (err) {
        console.error(err);
      }
    }

    async function loadTrades() {
      const body = document.getElementById("tradesBody");
      try {
        const trades = await fetchJson("/api/trades");
        body.innerHTML = "";

        if (!trades.length) {
          body.innerHTML =
            '<tr><td colspan="3" class="muted">No trades yet</td></tr>';
          return;
        }

        for (const t of trades) {
          const tr = document.createElement("tr");
          const pnl = Number(t.pnl_usd || 0);
          const cls = pnl >= 0 ? "pnl-pos" : "pnl-neg";

          tr.innerHTML = `
            <td>${t.time || ""}</td>
            <td>${t.market || ""}</td>
            <td class="${cls}">${formatUsd(pnl)}</td>
          `;
          body.appendChild(tr);
        }
      } catch (err) {
        console.error(err);
        body.innerHTML =
          '<tr><td colspan="3" class="muted">Error loading trades</td></tr>';
      }
    }

    async function loadEquity() {
      const canvas = document.getElementById("equityCanvas");
      const note = document.getElementById("equityNote");

      try {
        const points = await fetchJson("/api/equity");
        const ctx = canvas.getContext("2d");
        const width = canvas.width = canvas.offsetWidth;
        const height = canvas.height = canvas.offsetHeight;

        ctx.clearRect(0, 0, width, height);

        if (!points.length) {
          note.textContent = "Waiting for trades…";
          return;
        }

        note.textContent = "";

        const values = points.map(p => p.equity);
        const min = Math.min(...values);
        const max = Math.max(...values);

        const pad = 10;
        const xStep = (width - pad * 2) / Math.max(points.length - 1, 1);
        const range = max - min || 1;

        ctx.beginPath();
        ctx.strokeStyle = "#22c55e";
        ctx.lineWidth = 2;

        points.forEach((p, i) => {
          const x = pad + i * xStep;
          const yNorm = (p.equity - min) / range;
          const y = height - pad - yNorm * (height - pad * 2);

          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });

        ctx.stroke();

        // soft fill under curve
        ctx.lineTo(width - pad, height - pad);
        ctx.lineTo(pad, height - pad);
        ctx.closePath();
        ctx.fillStyle = "rgba(34,197,94,0.12)";
        ctx.fill();
      } catch (err) {
        console.error(err);
        note.textContent = "Error loading equity curve.";
      }
    }

    async function refreshAll() {
      await Promise.all([loadStats(), loadTrades(), loadEquity()]);
    }

    refreshAll();
    // Refresh every 60 seconds
    setInterval(refreshAll, 60000);
  </script>
</body>
</html>
"""

# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------


@app.route("/")
def index() -> Response:
    """Serve the dashboard HTML."""
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/api/status")
def api_status():
    """Simple health-check endpoint."""
    df = load_training_df()
    stats = compute_stats(df)
    return jsonify({"status": "ok", "events": stats["total_events"]})


@app.route("/api/stats")
def api_stats():
    """Stats endpoint used by the dashboard."""
    df = load_training_df()
    stats = compute_stats(df)
    return jsonify(stats)


@app.route("/data")
def data_compat():
    """Backward-compatible alias for /api/stats."""
    return api_stats()


@app.route("/api/trades")
def api_trades():
    """Recent trades derived from training_events.csv."""
    df = load_training_df()
    trades = training_events_to_trades(df, limit=20)
    return jsonify(trades)


@app.route("/api/equity")
def api_equity():
    """Equity curve derived from cumulative pnl_usd."""
    df = load_training_df()
    curve = training_events_to_equity_curve(df, starting_equity=1000.0)
    return jsonify(curve)


@app.route("/training-events", methods=["POST"])
@app.route("/training_events", methods=["POST"])  # support both just in case
def receive_training_event():
    """Endpoint the bot calls to send a training event."""
    try:
        event = request.get_json(force=True, silent=False)
        if not isinstance(event, dict):
            return jsonify({"error": "JSON body must be an object"}), 400
        append_training_event(event)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -------------------------------------------------------------------
# Render entrypoint
# -------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

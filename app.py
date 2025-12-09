from decimal import Decimal, getcontext
from pathlib import Path

from flask import Flask, jsonify, request, Response

import pandas as pd

app = Flask(__name__)

# High precision for PnL maths
getcontext().prec = 28

# -------------------------------------------------------------------
# Paths
# -------------------------------------------------------------------
DATA_DIR = Path("data")
TRAINING_FILE = DATA_DIR / "training_events.csv"
EQUITY_FILE = DATA_DIR / "equity_curve.csv"
TRADES_FILE = DATA_DIR / "trades.csv"


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def load_csv(path: Path):
    """Load a CSV file into a DataFrame, return (df, error)."""
    if not path.exists():
        return None, f"{path.name} not found"
    try:
        df = pd.read_csv(path)
        if df.empty:
            return None, f"{path.name} is empty"
        return df, None
    except Exception as e:
        return None, f"Error reading {path.name}: {e}"


def find_column(df: pd.DataFrame, keywords):
    """
    Try to find a column whose name contains any of the given keywords
    (case-insensitive). Returns column name or None.
    """
    lowered = {c.lower(): c for c in df.columns}
    for key in keywords:
        key = key.lower()
        for lc, real in lowered.items():
            if key in lc:
                return real
    return None


def compute_training_summary(df: pd.DataFrame):
    """Compute stats from training_events.csv."""
    wins = 0
    losses = 0
    total_pnl = Decimal("0")

    pnl_col = find_column(df, ["pnl_usd", "pnl"])
    if pnl_col is None:
        # No pnl column? just return counts.
        total = len(df)
        return {
            "total_events": int(total),
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl_usd": 0.0,
            "avg_pnl_usd": 0.0,
        }

    for _, row in df.iterrows():
        try:
            pnl = Decimal(str(row.get(pnl_col, "0")))
        except Exception:
            pnl = Decimal("0")

        total_pnl += pnl

        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

    total_trades = wins + losses if wins + losses > 0 else 1
    avg_pnl = total_pnl / Decimal(total_trades)

    return {
        "total_events": int(len(df)),
        "wins": int(wins),
        "losses": int(losses),
        "win_rate": float(Decimal(wins) / Decimal(total_trades)),
        "total_pnl_usd": float(total_pnl),
        "avg_pnl_usd": float(avg_pnl),
    }


def compute_equity_summary(df: pd.DataFrame):
    """Optional: simple summary from equity_curve.csv."""
    equity_col = find_column(df, ["equity", "balance"])
    if equity_col is None:
        return {}

    try:
        first = Decimal(str(df[equity_col].iloc[0]))
        last = Decimal(str(df[equity_col].iloc[-1]))
        change = last - first
        pct = (change / first) if first != 0 else Decimal("0")
        return {
            "equity_start": float(first),
            "equity_end": float(last),
            "equity_change_usd": float(change),
            "equity_change_pct": float(pct),
        }
    except Exception:
        return {}


# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------
@app.route("/")
def home():
    return "Crypto AI API Running 🚀"


@app.route("/data")
def data_summary():
    """
    Backwards-compatible summary endpoint.
    Uses training_events.csv only.
    """
    df, error = load_csv(TRAINING_FILE)
    if error:
        return jsonify({"error": error}), 500

    summary = compute_training_summary(df)
    return jsonify(summary), 200


@app.route("/stats")
def stats():
    """
    Richer stats endpoint, combining training_events and equity_curve if present.
    """
    result = {}

    # Training stats
    df_train, err_train = load_csv(TRAINING_FILE)
    if err_train is None:
        result["training"] = compute_training_summary(df_train)
    else:
        result["training_error"] = err_train

    # Equity stats
    df_equity, err_equity = load_csv(EQUITY_FILE)
    if err_equity is None:
        result["equity"] = compute_equity_summary(df_equity)
    else:
        result["equity_error"] = err_equity

    return jsonify(result), 200


@app.route("/equity")
def equity():
    """
    Return equity curve points from equity_curve.csv:
    [
      {"time": "...", "equity": 1000.0},
      ...
    ]
    """
    df, error = load_csv(EQUITY_FILE)
    if error:
        return jsonify({"error": error}), 500

    time_col = find_column(df, ["time", "timestamp", "date"])
    equity_col = find_column(df, ["equity", "balance"])

    if time_col is None or equity_col is None:
        return jsonify({"error": "Could not detect time/equity columns"}), 500

    points = []
    for _, row in df.iterrows():
        points.append(
            {
                "time": str(row[time_col]),
                "equity": float(row[equity_col]),
            }
        )

    return jsonify({"points": points}), 200


@app.route("/trades")
def trades():
    """
    Return recent trades from trades.csv (or fallback to training_events.csv).
    Supports ?limit=50
    """
    limit = request.args.get("limit", default=50, type=int)

    df, error = load_csv(TRADES_FILE)
    # Fallback: use training_events as "trades"
    if error:
        df, error = load_csv(TRAINING_FILE)
        if error:
            return jsonify({"error": f"{TRADES_FILE.name} and {TRAINING_FILE.name} not found"}), 500

    df = df.tail(limit)

    time_col = find_column(df, ["time", "timestamp", "entry_time"])
    market_col = find_column(df, ["market", "symbol", "pair"])
    pnl_col = find_column(df, ["pnl_usd", "pnl"])

    rows = []
    for _, row in df.iterrows():
        rows.append(
            {
                "time": str(row[time_col]) if time_col else "",
                "market": str(row[market_col]) if market_col else "",
                "pnl_usd": float(row[pnl_col]) if pnl_col else 0.0,
            }
        )

    return jsonify({"trades": rows}), 200


# -------------------------------------------------------------------
# Simple Dashboard
# -------------------------------------------------------------------
DASHBOARD_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Crypto AI Bot Dashboard</title>
  <style>
    body { font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif; margin: 20px; background: #0b0b10; color: #f5f5f5; }
    h1 { margin-bottom: 0.2rem; }
    .subtitle { color: #aaa; margin-bottom: 1.5rem; }
    .cards { display: flex; flex-wrap: wrap; gap: 1rem; margin-bottom: 1.5rem; }
    .card { background: #151520; padding: 1rem 1.25rem; border-radius: 0.75rem; min-width: 180px; box-shadow: 0 6px 16px rgba(0,0,0,0.4); }
    .card-title { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em; color: #9ca3af; }
    .card-value { font-size: 1.4rem; font-weight: 600; margin-top: 0.25rem; }
    .card-sub { font-size: 0.8rem; color: #6b7280; margin-top: 0.25rem; }
    canvas { background: #0f172a; border-radius: 0.75rem; padding: 0.5rem; }
    .grid { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1.4fr); gap: 1.5rem; align-items: start; }
    table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; font-size: 0.85rem; }
    th, td { padding: 0.35rem 0.5rem; text-align: left; }
    th { color: #9ca3af; border-bottom: 1px solid #1f2937; font-weight: 500; }
    tr:nth-child(even) { background: #0f172a; }
    .pnl-pos { color: #4ade80; }
    .pnl-neg { color: #f97373; }
    a { color: #38bdf8; text-decoration: none; }
    a:hover { text-decoration: underline; }
    @media (max-width: 900px) {
      .grid { grid-template-columns: minmax(0, 1fr); }
    }
  </style>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
  <h1>Crypto AI Bot Dashboard</h1>
  <div class="subtitle">
    Paper-trading stats from Render &mdash;
    <a href="/">API status</a> | <a href="/data">raw stats</a>
  </div>

  <div class="cards">
    <div class="card">
      <div class="card-title">Total Trades</div>
      <div class="card-value" id="stat-total-trades">–</div>
      <div class="card-sub" id="stat-wins-losses">–</div>
    </div>
    <div class="card">
      <div class="card-title">Win Rate</div>
      <div class="card-value" id="stat-win-rate">–</div>
      <div class="card-sub" id="stat-avg-pnl">–</div>
    </div>
    <div class="card">
      <div class="card-title">PnL (Total)</div>
      <div class="card-value" id="stat-total-pnl">–</div>
      <div class="card-sub" id="stat-equity-change">–</div>
    </div>
  </div>

  <div class="grid">
    <div>
      <h3>Equity Curve</h3>
      <canvas id="equityChart" height="120"></canvas>
    </div>
    <div>
      <h3>Recent Trades</h3>
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Market</th>
            <th>PnL (USD)</th>
          </tr>
        </thead>
        <tbody id="tradesBody">
          <tr><td colspan="3">Loading…</td></tr>
        </tbody>
      </table>
    </div>
  </div>

<script>
async function fetchJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error("Request failed: " + path);
  return await res.json();
}

function fmtUsd(v) {
  if (v === null || v === undefined) return "–";
  return (v >= 0 ? "+" : "–") + "$" + Math.abs(v).toFixed(2);
}

function fmtPct(v) {
  if (v === null || v === undefined) return "–";
  return (v >= 0 ? "+" : "–") + Math.abs(v * 100).toFixed(1) + "%";
}

async function loadStats() {
  try {
    const data = await fetchJson("/stats");
    const t = data.training || {};

    const total = t.total_events || 0;
    const wins = t.wins || 0;
    const losses = t.losses || 0;
    const winRate = t.win_rate || 0;
    const totalPnl = t.total_pnl_usd || 0;
    const avgPnl = t.avg_pnl_usd || 0;

    document.getElementById("stat-total-trades").textContent = total;
    document.getElementById("stat-wins-losses").textContent =
      wins + " wins / " + losses + " losses";

    document.getElementById("stat-win-rate").textContent = fmtPct(winRate);
    document.getElementById("stat-avg-pnl").textContent =
      "Avg " + fmtUsd(avgPnl) + " per trade";

    const e = data.equity || {};
    const eqChange = e.equity_change_usd;
    const eqPct = e.equity_change_pct;

    let eqText = "No equity data";
    if (eqChange !== undefined) {
      eqText = fmtUsd(eqChange) + " (" + fmtPct(eqPct) + ")";
    }
    document.getElementById("stat-total-pnl").textContent = fmtUsd(totalPnl);
    document.getElementById("stat-equity-change").textContent = eqText;
  } catch (err) {
    console.error(err);
  }
}

let equityChart;

async function loadEquity() {
  try {
    const data = await fetchJson("/equity");
    const points = data.points || [];
    const labels = points.map(p => p.time);
    const values = points.map(p => p.equity);

    const ctx = document.getElementById("equityChart").getContext("2d");
    if (equityChart) equityChart.destroy();
    equityChart = new Chart(ctx, {
      type: "line",
      data: {
        labels: labels,
        datasets: [{
          label: "Equity (USD)",
          data: values,
          tension: 0.2,
          borderWidth: 2,
          pointRadius: 0,
        }]
      },
      options: {
        plugins: {
          legend: { display: false }
        },
        scales: {
          x: { ticks: { display: false } }
        }
      }
    });
  } catch (err) {
    console.error(err);
  }
}

async function loadTrades() {
  try {
    const data = await fetchJson("/trades?limit=30");
    const trades = data.trades || [];
    const body = document.getElementById("tradesBody");
    body.innerHTML = "";

    if (trades.length === 0) {
      body.innerHTML = "<tr><td colspan='3'>No trades yet.</td></tr>";
      return;
    }

    for (const t of trades.reverse()) { // newest last
      const tr = document.createElement("tr");
      const pnl = t.pnl_usd || 0;
      const cls = pnl >= 0 ? "pnl-pos" : "pnl-neg";
      tr.innerHTML = `
        <td>${t.time || ""}</td>
        <td>${t.market || ""}</td>
        <td class="${cls}">${fmtUsd(pnl)}</td>
      `;
      body.appendChild(tr);
    }
  } catch (err) {
    console.error(err);
  }
}

async function init() {
  await Promise.all([
    loadStats(),
    loadEquity(),
    loadTrades()
  ]);
}

init();
</script>
</body>
</html>
"""


@app.route("/dashboard")
def dashboard():
    return Response(DASHBOARD_HTML, mimetype="text/html")


# -------------------------------------------------------------------
# Required for Render local runs
# -------------------------------------------------------------------
if __name__ == "__main__":
    # For local testing; Render will use gunicorn
    app.run(host="0.0.0.0", port=10000)

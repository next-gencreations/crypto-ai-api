"use client";

import { useEffect, useMemo, useState } from "react";

const REFRESH_MS = 5000;

function fmtMoney(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  return v.toLocaleString(undefined, { style: "currency", currency: "USD" });
}
function fmtNum(n, dp = 2) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  return Number(n).toFixed(dp);
}

function timeLeft(isoUtc) {
  if (!isoUtc) return "";
  const t = Date.parse(isoUtc);
  if (Number.isNaN(t)) return "";
  const ms = t - Date.now();
  if (ms <= 0) return "0s";
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const r = s % 60;
  return m > 0 ? `${m}m ${r}s` : `${r}s`;
}

/* --------- simple line chart (equity) --------- */
function MiniLineChart({ points, height = 150 }) {
  const w = 520;
  const h = height;

  const series = (points || []).filter((p) => typeof p?.equity_usd === "number");
  if (series.length < 2) {
    return <div style={{ height: h, display: "grid", placeItems: "center", opacity: 0.8 }}>NOT ENOUGH DATA</div>;
  }

  const ys = series.map((p) => p.equity_usd);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const pad = (maxY - minY) * 0.08 || 1;

  const yMin = minY - pad;
  const yMax = maxY + pad;

  const toX = (i) => (i / (series.length - 1)) * (w - 20) + 10;
  const toY = (y) => {
    const t = (y - yMin) / (yMax - yMin);
    return h - 10 - t * (h - 20);
  };

  const d = series.map((p, i) => `${i === 0 ? "M" : "L"} ${toX(i)} ${toY(p.equity_usd)}`).join(" ");

  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h}>
      <line x1="10" y1={h - 10} x2={w - 10} y2={h - 10} stroke="rgba(119,255,154,0.18)" />
      <line x1="10" y1={h / 2} x2={w - 10} y2={h / 2} stroke="rgba(119,255,154,0.10)" />
      <path d={d} fill="none" stroke="rgba(119,255,154,0.95)" strokeWidth="2.2" />
    </svg>
  );
}

/* --------- candle chart (your /ohlc) --------- */
function CandleChart({ candles, height = 240 }) {
  const w = 520;
  const h = height;
  const data = (candles || []).slice(-70);

  if (!data.length) {
    return <div style={{ height: h, display: "grid", placeItems: "center", opacity: 0.8 }}>NO CANDLES YET</div>;
  }

  const highs = data.map((c) => c.h);
  const lows = data.map((c) => c.l);
  const maxY = Math.max(...highs);
  const minY = Math.min(...lows);
  const pad = (maxY - minY) * 0.06 || 1;

  const yMax = maxY + pad;
  const yMin = minY - pad;

  const toY = (y) => {
    const t = (y - yMin) / (yMax - yMin);
    return h - 10 - t * (h - 20);
  };

  const bw = Math.max(4, Math.floor((w - 20) / data.length) - 1);

  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h}>
      <line x1="10" y1={h - 10} x2={w - 10} y2={h - 10} stroke="rgba(119,255,154,0.18)" />
      {data.map((c, i) => {
        const x = 10 + i * (bw + 1);
        const yO = toY(c.o);
        const yC = toY(c.c);
        const yH = toY(c.h);
        const yL = toY(c.l);
        const up = c.c >= c.o;

        const bodyTop = Math.min(yO, yC);
        const bodyBot = Math.max(yO, yC);
        const bodyH = Math.max(2, bodyBot - bodyTop);

        return (
          <g key={c.t}>
            <line
              x1={x + bw / 2}
              y1={yH}
              x2={x + bw / 2}
              y2={yL}
              stroke="rgba(119,255,154,0.55)"
              strokeWidth="1.1"
            />
            <rect
              x={x}
              y={bodyTop}
              width={bw}
              height={bodyH}
              fill={up ? "rgba(119,255,154,0.25)" : "rgba(119,255,154,0.08)"}
              stroke="rgba(119,255,154,0.85)"
              strokeWidth="1"
            />
          </g>
        );
      })}
    </svg>
  );
}

function TradingViewEmbed({ symbol = "BINANCE:BTCUSDT", interval = "5" }) {
  // TradingView widget embed via iframe (no scripts)
  const src =
    "https://s.tradingview.com/widgetembed/?" +
    new URLSearchParams({
      symbol,
      interval, // 1, 5, 15, 60, 240, D
      theme: "dark",
      style: "1", // candlesticks
      locale: "en",
      toolbarbg: "#06110a",
      enable_publishing: "false",
      hide_side_toolbar: "false",
      allow_symbol_change: "true",
      save_image: "false",
      studies: "",
    }).toString();

  return (
    <div className="pip-chartwrap" style={{ padding: 0, overflow: "hidden" }}>
      <iframe
        title="TradingView"
        src={src}
        style={{ width: "100%", height: 520, border: 0, display: "block" }}
        loading="lazy"
        referrerPolicy="no-referrer"
      />
    </div>
  );
}

export default function Page() {
  const apiBase = process.env.NEXT_PUBLIC_API_URL?.replace(/\/+$/, "") || "";
  const dataUrl = apiBase ? `${apiBase}/data` : "";

  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [payload, setPayload] = useState(null);
  const [lastFetchAt, setLastFetchAt] = useState(null);

  // Pages / tabs
  const [tab, setTab] = useState("STATUS"); // STATUS | DATA | LOG | CHARTS

  // Bot candles settings (your /ohlc)
  const [botIntervalSec, setBotIntervalSec] = useState(60);
  const [botMarket, setBotMarket] = useState("BTCUSDT");
  const [ohlc, setOhlc] = useState([]);

  // External crypto candles settings (TradingView)
  const [tvSymbol, setTvSymbol] = useState("BINANCE:BTCUSDT");
  const [tvInterval, setTvInterval] = useState("5");

  const heartbeat = payload?.heartbeat || {};
  const pet = payload?.pet || {};
  const control = payload?.control || {};
  const equity = payload?.equity || [];
  const trades = payload?.trades || [];
  const stateMode = String(payload?.state || "ACTIVE").toUpperCase();

  async function fetchJson(url, signal) {
    const res = await fetch(url, { cache: "no-store", signal });
    if (!res.ok) throw new Error(`API responded ${res.status}`);
    return res.json();
  }

  async function fetchData(signal) {
    if (!dataUrl) {
      setErr("Missing NEXT_PUBLIC_API_URL in Vercel environment variables.");
      setLoading(false);
      return;
    }
    try {
      setErr("");
      const json = await fetchJson(dataUrl, signal);
      setPayload(json);
      setLastFetchAt(new Date());

      // auto-pick first market for bot chart if available
      const m = json?.heartbeat?.markets;
      const first =
        Array.isArray(m) && m.length ? String(m[0]) :
        typeof m === "string" && m ? m :
        botMarket;

      if (first && first !== botMarket) setBotMarket(first);

      const o = await fetchJson(
        `${apiBase}/ohlc?market=${encodeURIComponent(first || botMarket)}&interval=${botIntervalSec}&limit=250`,
        signal
      );
      setOhlc(o?.candles || []);
    } catch (e) {
      if (e?.name === "AbortError") return;
      setErr(String(e?.message || e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    const ac = new AbortController();
    fetchData(ac.signal);

    const t = setInterval(() => {
      const ac2 = new AbortController();
      fetchData(ac2.signal);
      setTimeout(() => ac2.abort(), 8000);
    }, REFRESH_MS);

    return () => {
      ac.abort();
      clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataUrl, botIntervalSec]);

  async function postJson(path, body) {
    if (!apiBase) return;
    const res = await fetch(`${apiBase}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!res.ok) throw new Error(`POST ${path} failed: ${res.status}`);
    return res.json();
  }

  const pricesOk = heartbeat?.prices_ok === 1 || heartbeat?.prices_ok === true;
  const countdown =
    stateMode === "CRYO"
      ? timeLeft(control?.cryo_until_utc)
      : stateMode === "PAUSED"
      ? timeLeft(control?.pause_until_utc)
      : "";

  const sex = String(pet?.sex || "boy").toLowerCase();
  const petChar = sex === "girl" ? "VAULT GIRL" : "VAULT BOY";

  const statusBadge = useMemo(() => {
    if (stateMode === "CRYO") return "CRYO";
    if (stateMode === "PAUSED") return "PAUSED";
    return "ACTIVE";
  }, [stateMode]);

  return (
    <div className="pip-crt">
      <div className="pip-shell">
        <div className="pip-topbar">
          <div>
            <div className="pip-title">PIP-TRADE 3000</div>
            <div className="pip-sub wrap">
              API: {apiBase || "—"} · Refresh: {REFRESH_MS / 1000}s · Last:{" "}
              {lastFetchAt ? lastFetchAt.toLocaleTimeString() : "—"}
            </div>
          </div>

          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <span className="pip-badge">{statusBadge}</span>
            <span className="pip-badge">{pricesOk ? "PRICES OK" : "PRICES FAIL"}</span>
            {countdown ? <span className="pip-badge">THAW: {countdown}</span> : null}
          </div>
        </div>

        <div className="pip-tabs">
          <button className={`pip-tab ${tab === "STATUS" ? "active" : ""}`} onClick={() => setTab("STATUS")}>
            STATUS
          </button>
          <button className={`pip-tab ${tab === "DATA" ? "active" : ""}`} onClick={() => setTab("DATA")}>
            DATA
          </button>
          <button className={`pip-tab ${tab === "LOG" ? "active" : ""}`} onClick={() => setTab("LOG")}>
            LOG
          </button>
          <button className={`pip-tab ${tab === "CHARTS" ? "active" : ""}`} onClick={() => setTab("CHARTS")}>
            CHARTS
          </button>

          <div style={{ marginLeft: "auto", display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button className="pip-btn" onClick={() => fetchData(new AbortController().signal)}>
              REFRESH
            </button>
            <button
              className="pip-btn"
              onClick={async () => {
                try {
                  await postJson("/control/pause", { seconds: 600, reason: "Paused from Pip" });
                  await fetchData(new AbortController().signal);
                } catch {}
              }}
            >
              PAUSE
            </button>
            <button
              className="pip-btn"
              onClick={async () => {
                try {
                  await postJson("/control/cryo", { seconds: 600, reason: "Manual Cryo" });
                  await fetchData(new AbortController().signal);
                } catch {}
              }}
            >
              CRYO
            </button>
            <button
              className="pip-btn"
              onClick={async () => {
                try {
                  await postJson("/control/revive", { reason: "Revive" });
                  await fetchData(new AbortController().signal);
                } catch {}
              }}
            >
              REVIVE
            </button>
          </div>
        </div>

        {err && (
          <div className="pip-content">
            <div className="pip-panel">
              <div className="pip-heading">ERROR</div>
              <div className="wrap">{err}</div>
            </div>
          </div>
        )}

        <div className="pip-content">
          {/* STATUS */}
          {tab === "STATUS" && (
            <div className="pip-grid">
              <div className="pip-panel">
                <div className="pip-heading">SYSTEM STATUS</div>
                <div className="pip-row">
                  <div className="pip-k">Equity</div>
                  <div className="pip-v">{fmtMoney(heartbeat?.equity_usd)}</div>
                </div>
                <div className="pip-row">
                  <div className="pip-k">Markets</div>
                  <div className="pip-v wrap">
                    {Array.isArray(heartbeat?.markets) ? heartbeat.markets.join(", ") : heartbeat?.markets || "—"}
                  </div>
                </div>
                <div className="pip-row">
                  <div className="pip-k">Open positions</div>
                  <div className="pip-v">{heartbeat?.open_positions ?? "—"}</div>
                </div>
                <div className="pip-row">
                  <div className="pip-k">Survival</div>
                  <div className="pip-v">{heartbeat?.survival_mode || "—"}</div>
                </div>
                <div className="pip-row">
                  <div className="pip-k">Last heartbeat</div>
                  <div className="pip-v wrap">{heartbeat?.time_utc || "—"}</div>
                </div>
              </div>

              <div className="pip-panel">
                <div className="pip-heading">VAULT COMPANION</div>
                <div className="pip-row">
                  <div className="pip-k">Name</div>
                  <div className="pip-v">{petChar}</div>
                </div>
                <div className="pip-row">
                  <div className="pip-k">Stage</div>
                  <div className="pip-v">{pet?.stage || "—"}</div>
                </div>
                <div className="pip-row">
                  <div className="pip-k">Mood</div>
                  <div className="pip-v">{pet?.mood || "—"}</div>
                </div>
                <div className="pip-row">
                  <div className="pip-k">Health</div>
                  <div className="pip-v">{fmtNum(pet?.health, 1)}</div>
                </div>
                <div className="pip-row">
                  <div className="pip-k">Hunger</div>
                  <div className="pip-v">{fmtNum(pet?.hunger, 1)}</div>
                </div>
                <div className="pip-row">
                  <div className="pip-k">Growth</div>
                  <div className="pip-v">{fmtNum(pet?.growth, 1)}</div>
                </div>
                <div className="pip-row">
                  <div className="pip-k">Updated</div>
                  <div className="pip-v wrap">{pet?.time_utc || "—"}</div>
                </div>

                {stateMode === "CRYO" && (
                  <div className="pip-muted" style={{ marginTop: 10 }}>
                    CRYO TUBE ACTIVE: {control?.cryo_reason || "safety"} · THAW IN {countdown || "—"}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* DATA */}
          {tab === "DATA" && (
            <div className="pip-grid">
              <div className="pip-panel">
                <div className="pip-heading">EQUITY GRAPH</div>
                <div className="pip-chartwrap">
                  <MiniLineChart points={equity} />
                </div>
              </div>

              <div className="pip-panel">
                <div className="pip-heading">BOT PRICE CANDLES ({botMarket})</div>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
                  <button className={`pip-tab ${botIntervalSec === 60 ? "active" : ""}`} onClick={() => setBotIntervalSec(60)}>
                    1M
                  </button>
                  <button className={`pip-tab ${botIntervalSec === 300 ? "active" : ""}`} onClick={() => setBotIntervalSec(300)}>
                    5M
                  </button>
                  <button className={`pip-tab ${botIntervalSec === 900 ? "active" : ""}`} onClick={() => setBotIntervalSec(900)}>
                    15M
                  </button>
                </div>

                <div className="pip-chartwrap">
                  <CandleChart candles={ohlc} />
                </div>

                <div className="pip-muted" style={{ marginTop: 10 }}>
                  Candles are built from your bot’s /prices ticks → /ohlc
                </div>
              </div>
            </div>
          )}

          {/* LOG */}
          {tab === "LOG" && (
            <div className="pip-panel">
              <div className="pip-heading">TRADE LOG</div>

              <div style={{ overflowX: "auto" }}>
                <table className="pip-table">
                  <thead>
                    <tr>
                      <th>Time</th>
                      <th>Market</th>
                      <th>Side</th>
                      <th>Size</th>
                      <th>Price</th>
                      <th>PnL</th>
                      <th>Conf</th>
                      <th>Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(trades || []).slice(-25).reverse().map((t, idx) => (
                      <tr key={idx}>
                        <td className="wrap">{t.time_utc || "—"}</td>
                        <td>{t.market || "—"}</td>
                        <td>{t.side || "—"}</td>
                        <td>{fmtMoney(t.size_usd)}</td>
                        <td>{fmtNum(t.price, 2)}</td>
                        <td>{fmtMoney(t.pnl_usd)}</td>
                        <td>{fmtNum(t.confidence, 2)}</td>
                        <td className="wrap">{t.reason || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {!loading && (!trades || trades.length === 0) && (
                <div className="pip-muted" style={{ marginTop: 12 }}>
                  NO TRADES YET
                </div>
              )}
            </div>
          )}

          {/* CHARTS (new page) */}
          {tab === "CHARTS" && (
            <div className="pip-panel">
              <div className="pip-heading">CANDLE CHARTS</div>

              {/* Bot candles */}
              <div className="pip-panel" style={{ marginBottom: 14 }}>
                <div className="pip-heading">BOT CANDLES (YOUR STREAM) · {botMarket}</div>

                <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", marginBottom: 10 }}>
                  <span className="pip-muted">Interval:</span>
                  <button className={`pip-tab ${botIntervalSec === 60 ? "active" : ""}`} onClick={() => setBotIntervalSec(60)}>
                    1M
                  </button>
                  <button className={`pip-tab ${botIntervalSec === 300 ? "active" : ""}`} onClick={() => setBotIntervalSec(300)}>
                    5M
                  </button>
                  <button className={`pip-tab ${botIntervalSec === 900 ? "active" : ""}`} onClick={() => setBotIntervalSec(900)}>
                    15M
                  </button>

                  <span style={{ marginLeft: "auto" }} className="pip-muted">
                    Source: /ohlc?market=...&interval=...
                  </span>
                </div>

                <div className="pip-chartwrap">
                  <CandleChart candles={ohlc} height={280} />
                </div>
              </div>

              {/* External crypto candles */}
              <div className="pip-panel">
                <div className="pip-heading">CRYPTO CANDLES (EXTERNAL)</div>

                <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", marginBottom: 10 }}>
                  <span className="pip-muted">Symbol:</span>
                  <button className={`pip-tab ${tvSymbol === "BINANCE:BTCUSDT" ? "active" : ""}`} onClick={() => setTvSymbol("BINANCE:BTCUSDT")}>
                    BTC
                  </button>
                  <button className={`pip-tab ${tvSymbol === "BINANCE:ETHUSDT" ? "active" : ""}`} onClick={() => setTvSymbol("BINANCE:ETHUSDT")}>
                    ETH
                  </button>
                  <button className={`pip-tab ${tvSymbol === "BINANCE:SOLUSDT" ? "active" : ""}`} onClick={() => setTvSymbol("BINANCE:SOLUSDT")}>
                    SOL
                  </button>

                  <span className="pip-muted" style={{ marginLeft: 10 }}>TF:</span>
                  <button className={`pip-tab ${tvInterval === "1" ? "active" : ""}`} onClick={() => setTvInterval("1")}>
                    1M
                  </button>
                  <button className={`pip-tab ${tvInterval === "5" ? "active" : ""}`} onClick={() => setTvInterval("5")}>
                    5M
                  </button>
                  <button className={`pip-tab ${tvInterval === "15" ? "active" : ""}`} onClick={() => setTvInterval("15")}>
                    15M
                  </button>
                  <button className={`pip-tab ${tvInterval === "60" ? "active" : ""}`} onClick={() => setTvInterval("60")}>
                    1H
                  </button>
                </div>

                <TradingViewEmbed symbol={tvSymbol} interval={tvInterval} />

                <div className="pip-muted" style={{ marginTop: 10 }}>
                  External view is TradingView iframe. Bot candles remain your own stream-based truth.
                </div>
              </div>
            </div>
          )}
        </div>

        {loading && !payload && (
          <div className="pip-content">
            <div className="pip-muted">LOADING…</div>
          </div>
        )}
      </div>
    </div>
  );
}

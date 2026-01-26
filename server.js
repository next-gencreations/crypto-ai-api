import express from "express";
import cors from "cors";
import crypto from "crypto";
import fs from "fs";
import path from "path";
import https from "https";

const app = express();

// ---------- ENV ----------
const PORT = Number(process.env.PORT || 10000);

// Persisted storage directory on Render
const DATA_DIR = process.env.DATA_DIR || "/var/data";
const SETTINGS_FILE = process.env.SETTINGS_PATH || path.join(DATA_DIR, "settings.json");
const STATE_FILE = process.env.STATE_PATH || path.join(DATA_DIR, "state.json");

// Optional: used to show equity in USD
const GBPUSD_RATE = Number(process.env.GBPUSD_RATE || "1.27");

// CORS
const CORS_ORIGIN = process.env.CORS_ORIGIN || "*";

// Vault encryption key (IMPORTANT)
// Must be 32 bytes for AES-256, provided as base64.
// Generate locally:  node -e "console.log(require('crypto').randomBytes(32).toString('base64'))"
const VAULT_MASTER_KEY_B64 = process.env.VAULT_MASTER_KEY || "";
const VAULT_MASTER_KEY = safeMasterKey(VAULT_MASTER_KEY_B64); // Buffer(32) or null

// Vault session length (seconds)
const VAULT_TTL_SEC = Number(process.env.VAULT_TTL_SEC || "1800"); // 30 mins

// ---------- Helpers ----------
function ensureDir() {
  fs.mkdirSync(DATA_DIR, { recursive: true });
}

function readJson(file, fallback) {
  try {
    if (!fs.existsSync(file)) return fallback;
    const raw = fs.readFileSync(file, "utf-8");
    return JSON.parse(raw);
  } catch {
    return fallback;
  }
}

function writeJson(file, data) {
  ensureDir();
  fs.writeFileSync(file, JSON.stringify(data, null, 2));
}

function nowUtc() {
  return new Date().toISOString();
}

function toUnixMs(iso) {
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : 0;
}

function hashPin(pin) {
  return crypto.createHash("sha256").update(String(pin)).digest("hex");
}

function safeMasterKey(b64) {
  try {
    if (!b64) return null;
    const buf = Buffer.from(b64, "base64");
    if (buf.length !== 32) return null;
    return buf;
  } catch {
    return null;
  }
}

function defaultSettings() {
  return {
    vault_enabled: true,
    build_tag: process.env.NEXT_PUBLIC_BUILD_TAG || "v1",

    // trading/bot-ish config (safe defaults)
    bankroll_gbp: 1000,
    risk_per_trade_pct: 0.25,
    max_open_positions: 1,
    min_trade_interval_sec: 10,
    atr_period: 14,
    atr_stop_mult: 1.8,
    min_notional_usd: 25,
    max_notional_usd: 500,
    best_max_markets: 20,
    best_min_confidence: 0.3
  };
}

function defaultState() {
  return {
    system: {
      markets: "BTCUSDT, ETHUSDT",
      open_positions: 0,
      survival: "NORMAL",
      last_heartbeat_utc: null
    },
    bankroll: {
      amount_gbp: 1000
    },
    vault: {
      enabled: true,
      pin_set: false,
      locked: true,
      pin_hash: null,
      session_token: null,
      session_expires_utc: null,

      // encrypted key vault items
      // each item: { id, name, created_utc, enc: { v, iv, tag, data } }
      keys: []
    },
    events: [],
    trades: [],      // (for real trades later)
    sim_trades: []   // (paper trades)
  };
}

function loadSettings() {
  return { ...defaultSettings(), ...readJson(SETTINGS_FILE, {}) };
}

function saveSettings(s) {
  writeJson(SETTINGS_FILE, s);
}

function loadState() {
  const s = readJson(STATE_FILE, null);
  const base = defaultState();
  if (!s || typeof s !== "object") return base;

  // merge lightly (don’t destroy existing)
  return {
    ...base,
    ...s,
    system: { ...base.system, ...(s.system || {}) },
    bankroll: { ...base.bankroll, ...(s.bankroll || {}) },
    vault: { ...base.vault, ...(s.vault || {}) },
    events: Array.isArray(s.events) ? s.events : base.events,
    trades: Array.isArray(s.trades) ? s.trades : base.trades,
    sim_trades: Array.isArray(s.sim_trades) ? s.sim_trades : base.sim_trades
  };
}

function saveState(st) {
  writeJson(STATE_FILE, st);
}

function pushEvent(st, type, message, extra = {}) {
  st.events.push({
    time_utc: nowUtc(),
    type,
    message,
    ...extra
  });
  if (st.events.length > 200) st.events = st.events.slice(-200);
}

function vaultEnabled(settings, st) {
  const enabled = settings.vault_enabled !== false;
  st.vault.enabled = enabled;
  return enabled;
}

function isVaultUnlocked(st) {
  if (st.vault.locked) return false;
  if (!st.vault.session_token || !st.vault.session_expires_utc) return false;
  return Date.now() < toUnixMs(st.vault.session_expires_utc);
}

function vaultTtlSec(st) {
  if (!st.vault.session_expires_utc) return 0;
  const ms = toUnixMs(st.vault.session_expires_utc) - Date.now();
  return ms > 0 ? Math.floor(ms / 1000) : 0;
}

function requireVaultUnlocked(req, res, next) {
  const settings = loadSettings();
  const st = loadState();

  if (!vaultEnabled(settings, st)) {
    return res.status(403).json({ ok: false, error: "vault_disabled" });
  }

  // expire session if needed
  if (!isVaultUnlocked(st)) {
    st.vault.locked = true;
    st.vault.session_token = null;
    st.vault.session_expires_utc = null;
    saveState(st);

    return res.status(401).json({ ok: false, error: "vault_locked" });
  }

  const token = req.header("X-Vault-Token") || "";
  if (!token || token !== st.vault.session_token) {
    return res.status(401).json({ ok: false, error: "bad_token" });
  }

  // attach state to request for convenience
  req._state = st;
  req._settings = settings;
  next();
}

// ---------- Encryption (AES-256-GCM) ----------
function encryptJson(obj) {
  if (!VAULT_MASTER_KEY) {
    // Still allow running, but warn in responses and logs.
    // Encryption disabled => store plaintext (NOT recommended).
    return { v: 0, iv: null, tag: null, data: Buffer.from(JSON.stringify(obj), "utf8").toString("base64") };
  }

  const iv = crypto.randomBytes(12); // GCM 12 bytes
  const cipher = crypto.createCipheriv("aes-256-gcm", VAULT_MASTER_KEY, iv);
  const plaintext = Buffer.from(JSON.stringify(obj), "utf8");
  const enc = Buffer.concat([cipher.update(plaintext), cipher.final()]);
  const tag = cipher.getAuthTag();

  return {
    v: 1,
    iv: iv.toString("base64"),
    tag: tag.toString("base64"),
    data: enc.toString("base64")
  };
}

function decryptJson(encObj) {
  if (!encObj || typeof encObj !== "object") throw new Error("bad_enc");

  // plaintext fallback mode
  if (encObj.v === 0) {
    const raw = Buffer.from(encObj.data, "base64").toString("utf8");
    return JSON.parse(raw);
  }

  if (!VAULT_MASTER_KEY) throw new Error("missing_master_key");

  const iv = Buffer.from(encObj.iv, "base64");
  const tag = Buffer.from(encObj.tag, "base64");
  const data = Buffer.from(encObj.data, "base64");

  const decipher = crypto.createDecipheriv("aes-256-gcm", VAULT_MASTER_KEY, iv);
  decipher.setAuthTag(tag);

  const dec = Buffer.concat([decipher.update(data), decipher.final()]);
  return JSON.parse(dec.toString("utf8"));
}

function mask(str, keep = 4) {
  const s = String(str || "");
  if (s.length <= keep) return "*".repeat(s.length);
  return "*".repeat(Math.max(0, s.length - keep)) + s.slice(-keep);
}

// ---------- HTTP helpers (Binance candles) ----------
const candleCache = new Map(); // key => { expires, payload }

function httpsGetJson(url) {
  return new Promise((resolve, reject) => {
    https
      .get(url, { headers: { "User-Agent": "crypto-ai-api/1.0" } }, (resp) => {
        let data = "";
        resp.on("data", (chunk) => (data += chunk));
        resp.on("end", () => {
          try {
            const json = JSON.parse(data);
            resolve({ status: resp.statusCode || 200, json });
          } catch (e) {
            reject(new Error("bad_json"));
          }
        });
      })
      .on("error", reject);
  });
}

function normalizeInterval(interval) {
  // UI sometimes sends "60" for 60 minutes
  const s = String(interval || "").trim();

  // already Binance-style
  const allowed = new Set(["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"]);
  if (allowed.has(s)) return s;

  // numeric minutes -> map
  const n = Number(s);
  if (Number.isFinite(n) && n > 0) {
    if (n === 1) return "1m";
    if (n === 3) return "3m";
    if (n === 5) return "5m";
    if (n === 15) return "15m";
    if (n === 30) return "30m";
    if (n === 60) return "1h";
    if (n === 120) return "2h";
    if (n === 240) return "4h";
    if (n === 1440) return "1d";
  }

  // safe default
  return "1h";
}

// ---------- Middleware ----------
app.use(express.json({ limit: "1mb" }));
app.use(
  cors({
    origin: CORS_ORIGIN === "*" ? true : CORS_ORIGIN,
    credentials: true
  })
);

// ---------- Routes ----------

// Root
app.get("/", (_req, res) => {
  res.json({ ok: true, service: "crypto-ai-api" });
});

// Health
app.get("/health", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();
  const enabled = vaultEnabled(settings, st);

  // expire session if needed
  if (!isVaultUnlocked(st)) {
    st.vault.locked = true;
    st.vault.session_token = null;
    st.vault.session_expires_utc = null;
    saveState(st);
  }

  res.json({
    ok: true,
    status: 200,
    service: "crypto-ai-api",
    time_utc: nowUtc(),
    vault_enabled: enabled,
    vault_unlocked: enabled ? isVaultUnlocked(st) : false
  });
});

// Settings
app.get("/settings", (_req, res) => {
  res.json(loadSettings());
});

app.post("/settings", (req, res) => {
  const current = loadSettings();
  const patch = req.body && typeof req.body === "object" ? req.body : {};
  const merged = { ...current, ...patch };

  // keep bankroll in sync with settings if provided
  if (patch.bankroll_gbp != null) {
    merged.bankroll_gbp = Number(patch.bankroll_gbp);
  }

  saveSettings(merged);

  // also mirror bankroll into state (so UI stays consistent)
  const st = loadState();
  if (Number.isFinite(merged.bankroll_gbp)) {
    st.bankroll.amount_gbp = Number(merged.bankroll_gbp);
  }
  saveState(st);

  res.json({ ok: true, settings: merged });
});

// System status
app.get("/status", (_req, res) => {
  const st = loadState();
  const bankroll = st.bankroll?.amount_gbp ?? 1000;
  const equityUsd = (Number(bankroll) * GBPUSD_RATE).toFixed(2);

  res.json({
    ok: true,
    markets: st.system.markets,
    open_positions: st.system.open_positions,
    survival: st.system.survival,
    equity_usd: Number(equityUsd),
    last_heartbeat_utc: st.system.last_heartbeat_utc
  });
});

// Bankroll
app.get("/bankroll", (_req, res) => {
  const st = loadState();
  res.json({ ok: true, amount_gbp: st.bankroll.amount_gbp });
});

app.post("/bankroll", (req, res) => {
  const amount = Number(req.body?.amount_gbp ?? req.body?.amount ?? req.body?.value);
  if (!Number.isFinite(amount) || amount < 0) {
    return res.status(400).json({ ok: false, error: "invalid_amount" });
  }

  const st = loadState();
  st.bankroll.amount_gbp = amount;
  st.system.last_heartbeat_utc = nowUtc();
  pushEvent(st, "bankroll", `Bankroll set to £${amount}`);
  saveState(st);

  // also mirror into settings for your bot config page
  const settings = loadSettings();
  settings.bankroll_gbp = amount;
  saveSettings(settings);

  res.json({ ok: true, amount_gbp: amount });
});

// Data (dashboard)
app.get("/data", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();

  const bankroll = st.bankroll?.amount_gbp ?? settings.bankroll_gbp ?? 1000;
  const equityUsd = (Number(bankroll) * GBPUSD_RATE).toFixed(2);

  const enabled = vaultEnabled(settings, st);

  // expire session if needed
  if (!isVaultUnlocked(st)) {
    st.vault.locked = true;
    st.vault.session_token = null;
    st.vault.session_expires_utc = null;
    saveState(st);
  }

  res.json({
    ok: true,
    time_utc: nowUtc(),
    equity_usd: Number(equityUsd),
    bankroll_gbp: bankroll,
    markets: st.system.markets,
    survival: st.system.survival,
    open_positions: st.system.open_positions,
    events: st.events || [],
    trades: st.trades || [],
    sim_trades: st.sim_trades || [],
    vault: {
      enabled,
      pin_set: !!st.vault.pin_set,
      locked: enabled ? !isVaultUnlocked(st) : true,
      unlocked: enabled ? isVaultUnlocked(st) : false,
      ttl_sec: enabled ? vaultTtlSec(st) : 0,
      encryption: VAULT_MASTER_KEY ? "aes-256-gcm" : "PLAINTEXT_NO_MASTER_KEY"
    }
  });
});

// Logs
app.get("/logs", (_req, res) => {
  const st = loadState();
  res.json({
    ok: true,
    lines: (st.events || []).slice(-120).map((e) => `${e.time_utc} | ${e.type} | ${e.message}`)
  });
});

// ---------- B) Live OHLC candles ----------
app.get("/ohlc", async (req, res) => {
  try {
    const market = String(req.query.market || "BTCUSDT").toUpperCase().replace(/[^A-Z0-9]/g, "");
    const intervalRaw = String(req.query.interval || "60");
    const interval = normalizeInterval(intervalRaw);
    const limit = Math.max(10, Math.min(1000, Number(req.query.limit || "300")));

    const cacheKey = `${market}:${interval}:${limit}`;
    const cached = candleCache.get(cacheKey);
    if (cached && cached.expires > Date.now()) {
      return res.json(cached.payload);
    }

    const url = `https://api.binance.com/api/v3/klines?symbol=${encodeURIComponent(market)}&interval=${encodeURIComponent(
      interval
    )}&limit=${encodeURIComponent(String(limit))}`;

    const { status, json } = await httpsGetJson(url);
    if (status !== 200 || !Array.isArray(json)) {
      return res.status(502).json({ ok: false, error: "binance_error", status, raw: json });
    }

    // Binance kline format:
    // [ openTime, open, high, low, close, volume, closeTime, quoteAssetVolume, trades, takerBaseVol, takerQuoteVol, ignore ]
    const candles = json.map((k) => ({
      t: Number(k[0]),
      o: Number(k[1]),
      h: Number(k[2]),
      l: Number(k[3]),
      c: Number(k[4]),
      v: Number(k[5])
    }));

    const payload = { ok: true, market, interval, limit, candles };
    candleCache.set(cacheKey, { expires: Date.now() + 10_000, payload }); // cache 10s
    res.json(payload);
  } catch (e) {
    res.status(500).json({ ok: false, error: "ohlc_failed" });
  }
});

// ---------- Vault (A) ----------

// Status (GET)
app.get("/vault/status", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();
  const enabled = vaultEnabled(settings, st);

  // expire session if needed
  if (!isVaultUnlocked(st)) {
    st.vault.locked = true;
    st.vault.session_token = null;
    st.vault.session_expires_utc = null;
    saveState(st);
  }

  res.json({
    ok: true,
    enabled,
    pin_set: !!st.vault.pin_set,
    unlocked: enabled ? isVaultUnlocked(st) : false,
    ttl_sec: enabled ? vaultTtlSec(st) : 0
  });
});

// Set PIN (POST)  (matches your dashboard: /vault/pin/set)
app.post("/vault/pin/set", (req, res) => {
  const settings = loadSettings();
  const st = loadState();

  if (!vaultEnabled(settings, st)) {
    return res.status(403).json({ ok: false, error: "vault_disabled" });
  }

  const pin = String(req.body?.pin || "");
  if (pin.length < 4 || pin.length > 8) {
    return res.status(400).json({ ok: false, error: "invalid_pin" });
  }

  st.vault.pin_hash = hashPin(pin);
  st.vault.pin_set = true;

  // after setting pin, keep locked until user unlocks
  st.vault.locked = true;
  st.vault.session_token = null;
  st.vault.session_expires_utc = null;

  pushEvent(st, "vault", "PIN set");
  saveState(st);

  res.json({ ok: true });
});

// Unlock (POST)  (matches your dashboard: /vault/unlock)
app.post("/vault/unlock", (req, res) => {
  const settings = loadSettings();
  const st = loadState();

  if (!vaultEnabled(settings, st)) {
    return res.status(403).json({ ok: false, error: "vault_disabled" });
  }

  const pin = String(req.body?.pin || "");
  if (!st.vault.pin_set || !st.vault.pin_hash) {
    return res.status(400).json({ ok: false, error: "pin_not_set" });
  }

  if (hashPin(pin) !== st.vault.pin_hash) {
    return res.status(401).json({ ok: false, error: "bad_pin" });
  }

  st.vault.locked = false;
  st.vault.session_token = crypto.randomBytes(16).toString("hex");
  st.vault.session_expires_utc = new Date(Date.now() + VAULT_TTL_SEC * 1000).toISOString();

  pushEvent(st, "vault", "Vault unlocked");
  saveState(st);

  res.json({
    ok: true,
    token: st.vault.session_token,
    ttl_sec: vaultTtlSec(st),
    encryption: VAULT_MASTER_KEY ? "aes-256-gcm" : "PLAINTEXT_NO_MASTER_KEY"
  });
});

// Lock (POST)  (matches your dashboard: /vault/lock)
app.post("/vault/lock", (_req, res) => {
  const st = loadState();

  st.vault.locked = true;
  st.vault.session_token = null;
  st.vault.session_expires_utc = null;

  pushEvent(st, "vault", "Vault locked");
  saveState(st);

  res.json({ ok: true });
});

// ----- A) Key Vault endpoints -----
// List keys (masked) (GET)
app.get("/vault/keys", requireVaultUnlocked, (req, res) => {
  const st = req._state;

  const items = (st.vault.keys || []).map((k) => ({
    id: k.id,
    name: k.name,
    created_utc: k.created_utc
  }));

  res.json({
    ok: true,
    keys: items,
    ttl_sec: vaultTtlSec(st),
    encryption: VAULT_MASTER_KEY ? "aes-256-gcm" : "PLAINTEXT_NO_MASTER_KEY"
  });
});

// Add / update a key (POST)
// body: { name: "binance", api_key: "...", api_secret: "..." }
app.post("/vault/keys", requireVaultUnlocked, (req, res) => {
  const st = req._state;

  const name = String(req.body?.name || "").trim().toLowerCase();
  const apiKey = String(req.body?.api_key || "").trim();
  const apiSecret = String(req.body?.api_secret || "").trim();

  if (!name || name.length > 40) return res.status(400).json({ ok: false, error: "bad_name" });
  if (!apiKey || apiKey.length < 8) return res.status(400).json({ ok: false, error: "bad_api_key" });
  if (!apiSecret || apiSecret.length < 8) return res.status(400).json({ ok: false, error: "bad_api_secret" });

  const id = crypto.randomBytes(8).toString("hex");
  const enc = encryptJson({ apiKey, apiSecret });

  const item = {
    id,
    name,
    created_utc: nowUtc(),
    enc
  };

  st.vault.keys = Array.isArray(st.vault.keys) ? st.vault.keys : [];
  st.vault.keys.push(item);

  pushEvent(st, "vault", `Key added: ${name}`);
  saveState(st);

  res.json({
    ok: true,
    id,
    name,
    created_utc: item.created_utc,
    ttl_sec: vaultTtlSec(st),
    encryption: VAULT_MASTER_KEY ? "aes-256-gcm" : "PLAINTEXT_NO_MASTER_KEY"
  });
});

// Delete key (DELETE)
app.delete("/vault/keys/:id", requireVaultUnlocked, (req, res) => {
  const st = req._state;
  const id = String(req.params.id || "");

  const before = st.vault.keys?.length || 0;
  st.vault.keys = (st.vault.keys || []).filter((k) => k.id !== id);
  const after = st.vault.keys.length;

  if (after === before) return res.status(404).json({ ok: false, error: "not_found" });

  pushEvent(st, "vault", `Key removed: ${id}`);
  saveState(st);

  res.json({ ok: true, ttl_sec: vaultTtlSec(st) });
});

// (Optional) Use internally: decrypt a key by name (NOT exposed to UI)
function getDecryptedKeyByName(st, name) {
  const item = (st.vault.keys || []).find((k) => k.name === name);
  if (!item) return null;
  return decryptJson(item.enc);
}

// ---------- C) Simulation Trading ----------

// List sim trades
app.get("/sim/trades", (_req, res) => {
  const st = loadState();
  res.json({ ok: true, sim_trades: st.sim_trades || [] });
});

// Place a simulated trade
// body: { market:"BTCUSDT", side:"BUY"|"SELL", qty:0.01, price: optional }
// If price omitted, uses last candle close (if available) OR 0
app.post("/sim/trade", async (req, res) => {
  const st = loadState();

  const market = String(req.body?.market || "BTCUSDT").toUpperCase().replace(/[^A-Z0-9]/g, "");
  const side = String(req.body?.side || "BUY").toUpperCase();
  const qty = Number(req.body?.qty ?? req.body?.quantity ?? 0);

  if (!market) return res.status(400).json({ ok: false, error: "bad_market" });
  if (side !== "BUY" && side !== "SELL") return res.status(400).json({ ok: false, error: "bad_side" });
  if (!Number.isFinite(qty) || qty <= 0) return res.status(400).json({ ok: false, error: "bad_qty" });

  let price = Number(req.body?.price ?? 0);
  if (!Number.isFinite(price) || price <= 0) {
    // try fetch last close quickly
    try {
      const interval = "1m";
      const url = `https://api.binance.com/api/v3/klines?symbol=${encodeURIComponent(market)}&interval=${interval}&limit=1`;
      const { status, json } = await httpsGetJson(url);
      if (status === 200 && Array.isArray(json) && json[0]) {
        price = Number(json[0][4]) || 0;
      }
    } catch {}
  }

  const trade = {
    id: crypto.randomBytes(8).toString("hex"),
    time_utc: nowUtc(),
    market,
    side,
    qty,
    price,
    notional: price > 0 ? Number((qty * price).toFixed(8)) : 0,
    mode: "SIM"
  };

  st.sim_trades = Array.isArray(st.sim_trades) ? st.sim_trades : [];
  st.sim_trades.push(trade);
  if (st.sim_trades.length > 500) st.sim_trades = st.sim_trades.slice(-500);

  pushEvent(st, "sim", `SIM ${side} ${qty} ${market} @ ${price || "MKT"}`);
  saveState(st);

  res.json({ ok: true, trade });
});

// Clear sim trades (optional convenience)
app.post("/sim/trades/clear", (req, res) => {
  const st = loadState();
  st.sim_trades = [];
  pushEvent(st, "sim", "SIM trades cleared");
  saveState(st);
  res.json({ ok: true });
});

// ---------- Start ----------
app.listen(PORT, "0.0.0.0", () => {
  ensureDir();
  console.log(`crypto-ai-api listening on :${PORT}`);
  if (!VAULT_MASTER_KEY) {
    console.warn("WARNING: VAULT_MASTER_KEY not set (vault keys stored in plaintext base64). Set VAULT_MASTER_KEY!");
  }
});

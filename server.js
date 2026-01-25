import express from "express";
import cors from "cors";
import crypto from "crypto";
import fs from "fs";
import path from "path";

const app = express();

// ---------- ENV ----------
const PORT = Number(process.env.PORT || 10000);

// Persisted storage directory on Render
const DATA_DIR = process.env.DATA_DIR || "/var/data";
const SETTINGS_FILE = process.env.SETTINGS_PATH || path.join(DATA_DIR, "settings.json");
const STATE_FILE = process.env.STATE_PATH || path.join(DATA_DIR, "state.json");

const GBPUSD_RATE = Number(process.env.GBPUSD_RATE || "1.27");

// CORS
const CORS_ORIGIN = process.env.CORS_ORIGIN || "*";

// Vault session length (seconds) – dashboard shows TTL from this
const VAULT_SESSION_TTL_SEC = Number(process.env.VAULT_SESSION_TTL_SEC || "1800"); // 30 min

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

function hashPin(pin) {
  return crypto.createHash("sha256").update(String(pin)).digest("hex");
}

function defaultSettings() {
  return {
    vault_enabled: true,
    build_tag: process.env.NEXT_PUBLIC_BUILD_TAG || "v1",
  };
}

function defaultState() {
  return {
    system: {
      markets: "BTCUSDT, ETHUSDT",
      open_positions: 0,
      survival: "NORMAL",
      last_heartbeat_utc: null,
    },
    bankroll: {
      amount_gbp: 1000,
    },
    vault: {
      enabled: true,
      pin_set: false,
      locked: true,
      pin_hash: null,

      // session
      session_token: null,
      session_expires_utc: null,
    },
    events: [],
    trades: [],
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
  return s && typeof s === "object" ? s : defaultState();
}

function saveState(st) {
  writeJson(STATE_FILE, st);
}

function pushEvent(st, type, message, extra = {}) {
  st.events.push({
    time_utc: nowUtc(),
    type,
    message,
    ...extra,
  });
  if (st.events.length > 200) st.events = st.events.slice(-200);
}

function parseUtc(iso) {
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : null;
}

function vaultEnabled(settings) {
  return settings.vault_enabled !== false;
}

// If session expired, lock it.
function applyVaultExpiry(st) {
  if (!st?.vault) return;

  if (!st.vault.session_expires_utc) return;

  const exp = parseUtc(st.vault.session_expires_utc);
  if (!exp) return;

  if (Date.now() >= exp) {
    st.vault.locked = true;
    st.vault.session_token = null;
    st.vault.session_expires_utc = null;
  }
}

function vaultTtlSec(st) {
  applyVaultExpiry(st);
  if (st.vault.locked) return 0;
  const exp = parseUtc(st.vault.session_expires_utc);
  if (!exp) return 0;
  const remaining = Math.floor((exp - Date.now()) / 1000);
  return remaining > 0 ? remaining : 0;
}

function setUnlockedSession(st) {
  st.vault.locked = false;
  st.vault.session_token = crypto.randomBytes(16).toString("hex");
  st.vault.session_expires_utc = new Date(Date.now() + VAULT_SESSION_TTL_SEC * 1000).toISOString();
}

// ---------- Middleware ----------
app.use(express.json({ limit: "1mb" }));
app.use(
  cors({
    origin: CORS_ORIGIN === "*" ? true : CORS_ORIGIN,
    credentials: true,
    allowedHeaders: ["Content-Type", "X-Vault-Token"],
    methods: ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
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
  applyVaultExpiry(st);

  const enabled = vaultEnabled(settings);

  res.json({
    ok: true,
    status: 200,
    service: "crypto-ai-api",
    time_utc: nowUtc(),
    vault_enabled: enabled,
    vault_unlocked: enabled ? !st.vault.locked : false,
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
  saveSettings(merged);
  res.json({ ok: true, settings: merged });
});

// Status
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
    last_heartbeat_utc: st.system.last_heartbeat_utc,
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

  res.json({ ok: true, amount_gbp: amount });
});

// Data
app.get("/data", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();
  applyVaultExpiry(st);

  const bankroll = st.bankroll?.amount_gbp ?? 1000;
  const equityUsd = (Number(bankroll) * GBPUSD_RATE).toFixed(2);

  const enabled = vaultEnabled(settings);

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
    vault: {
      enabled,
      pin_set: !!st.vault.pin_set,
      locked: enabled ? !!st.vault.locked : true,
      // these help the dashboard too if it reads vault from /data
      unlocked: enabled ? !st.vault.locked : false,
      ttl_sec: enabled ? vaultTtlSec(st) : 0,
    },
  });
});

// Logs
app.get("/logs", (_req, res) => {
  const st = loadState();
  res.json({
    ok: true,
    lines: (st.events || []).slice(-120).map((e) => `${e.time_utc} | ${e.type} | ${e.message}`),
  });
});

// OHLC (stub)
app.get("/ohlc", (req, res) => {
  const market = String(req.query.market || "BTCUSDT");
  const interval = String(req.query.interval || "60");
  const limit = Number(req.query.limit || "300");

  res.json({
    ok: true,
    market,
    interval,
    limit,
    candles: [],
  });
});

// ---------- Vault (Dashboard-compatible routes) ----------

// IMPORTANT: dashboard expects unlocked + ttl_sec here
app.get("/vault/status", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();
  applyVaultExpiry(st);

  const enabled = vaultEnabled(settings);
  const unlocked = enabled ? !st.vault.locked : false;

  res.json({
    ok: true,
    enabled,
    pin_set: !!st.vault.pin_set,
    unlocked,
    ttl_sec: enabled ? vaultTtlSec(st) : 0,
  });
});

// Dashboard calls: POST /vault/pin/set { pin }
app.post("/vault/pin/set", (req, res) => {
  const settings = loadSettings();
  if (!vaultEnabled(settings)) {
    return res.status(403).json({ ok: false, error: "vault_disabled" });
  }

  const pin = String(req.body?.pin || "");
  if (pin.length < 4 || pin.length > 8) {
    return res.status(400).json({ ok: false, error: "invalid_pin" });
  }

  const st = loadState();
  st.vault.pin_hash = hashPin(pin);
  st.vault.pin_set = true;

  // after setting a PIN, keep it locked until they unlock
  st.vault.locked = true;
  st.vault.session_token = null;
  st.vault.session_expires_utc = null;

  pushEvent(st, "vault", "PIN set");
  saveState(st);

  res.json({ ok: true });
});

// Dashboard calls: POST /vault/unlock { pin } -> expects token + ttl_sec
app.post("/vault/unlock", (req, res) => {
  const settings = loadSettings();
  if (!vaultEnabled(settings)) {
    return res.status(403).json({ ok: false, error: "vault_disabled" });
  }

  const pin = String(req.body?.pin || "");
  const st = loadState();
  applyVaultExpiry(st);

  if (!st.vault.pin_set || !st.vault.pin_hash) {
    return res.status(400).json({ ok: false, error: "pin_not_set" });
  }

  if (hashPin(pin) !== st.vault.pin_hash) {
    return res.status(401).json({ ok: false, error: "bad_pin" });
  }

  setUnlockedSession(st);
  pushEvent(st, "vault", "Vault unlocked");
  saveState(st);

  res.json({
    ok: true,
    token: st.vault.session_token,
    ttl_sec: vaultTtlSec(st),
    expires_utc: st.vault.session_expires_utc,
  });
});

// Dashboard calls: POST /vault/lock
app.post("/vault/lock", (_req, res) => {
  const st = loadState();
  st.vault.locked = true;
  st.vault.session_token = null;
  st.vault.session_expires_utc = null;
  pushEvent(st, "vault", "Vault locked");
  saveState(st);
  res.json({ ok: true });
});

// ---------- Legacy routes (keep for compatibility) ----------

app.post("/vault/set-pin", (req, res) => {
  // same behavior as /vault/pin/set
  req.url = "/vault/pin/set";
  return app._router.handle(req, res, () => {});
});

app.post("/vault/use-pin", (req, res) => {
  // same behavior as /vault/unlock
  req.url = "/vault/unlock";
  return app._router.handle(req, res, () => {});
});

// ---------- Start ----------
app.listen(PORT, "0.0.0.0", () => {
  ensureDir();
  console.log(`crypto-ai-api listening on :${PORT}`);
});

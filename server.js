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

function vaultEnabledOr403(res) {
  const settings = loadSettings();
  if (settings.vault_enabled === false) {
    res.status(403).json({ ok: false, error: "vault_disabled" });
    return false;
  }
  return true;
}

function normalizePinInputs(body) {
  // Dashboard may send different keys. We accept all of them.
  const pin =
    body?.pin ??
    body?.current_pin ??
    body?.old_pin ??
    body?.existing_pin ??
    "";

  const newPin =
    body?.new_pin ??
    body?.newPin ??
    body?.pin_new ??
    body?.set_pin ??
    body?.pin ??
    "";

  return { pin: String(pin), newPin: String(newPin) };
}

function validatePinFormat(pin) {
  // Keep it simple: 4–8 digits (adjust if you want)
  if (!pin) return false;
  if (pin.length < 4 || pin.length > 8) return false;
  if (!/^\d+$/.test(pin)) return false;
  return true;
}

function makeSession(st, minutes = 30) {
  st.vault.session_token = crypto.randomBytes(16).toString("hex");
  st.vault.session_expires_utc = new Date(Date.now() + minutes * 60 * 1000).toISOString();
  return { token: st.vault.session_token, expires_utc: st.vault.session_expires_utc };
}

// ---------- Middleware ----------
app.use(express.json({ limit: "1mb" }));
app.use(
  cors({
    origin: CORS_ORIGIN === "*" ? true : CORS_ORIGIN,
    credentials: true,
  })
);

// Always respond to OPTIONS (helps Vercel/proxy/browser)
app.options("*", (_req, res) => res.sendStatus(204));

// ---------- Routes ----------

// Root
app.get("/", (_req, res) => {
  res.json({ ok: true, service: "crypto-ai-api" });
});

// Health
app.get("/health", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();
  const vaultEnabled = settings.vault_enabled !== false;

  res.json({
    ok: true,
    status: 200,
    service: "crypto-ai-api",
    time_utc: nowUtc(),
    vault_enabled: vaultEnabled,
    vault_unlocked: vaultEnabled ? !st.vault.locked : false,
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

  const bankroll = st.bankroll?.amount_gbp ?? 1000;
  const equityUsd = (Number(bankroll) * GBPUSD_RATE).toFixed(2);

  const vaultEnabled = settings.vault_enabled !== false;

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
      enabled: vaultEnabled,
      pin_set: !!st.vault.pin_set,
      locked: vaultEnabled ? !!st.vault.locked : true,
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

// OHLC (placeholder)
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

// ---------- Vault ----------

app.get("/vault/status", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();
  const enabled = settings.vault_enabled !== false;
  const locked = enabled ? !!st.vault.locked : true;

  res.json({
    ok: true,
    enabled,
    pin_set: !!st.vault.pin_set,
    locked,
  });
});

// NEW: Dashboard endpoint(s) for setting pin
// Supports: POST /vault/pin/set  and POST /vault/pin (alias)
function handleSetPin(req, res) {
  if (!vaultEnabledOr403(res)) return;

  const st = loadState();
  const { pin, newPin } = normalizePinInputs(req.body);

  // If PIN not set yet: accept newPin (or pin if they send just "pin")
  if (!st.vault.pin_set) {
    if (!validatePinFormat(newPin)) {
      return res.status(400).json({ ok: false, error: "invalid_pin" });
    }
    st.vault.pin_hash = hashPin(newPin);
    st.vault.pin_set = true;
    st.vault.locked = true;
    st.vault.session_token = null;
    st.vault.session_expires_utc = null;
    pushEvent(st, "vault", "PIN set");
    saveState(st);
    return res.json({ ok: true });
  }

  // If PIN already set: require current pin to change it
  if (!validatePinFormat(pin) || hashPin(pin) !== st.vault.pin_hash) {
    return res.status(401).json({ ok: false, error: "bad_pin" });
  }
  if (!validatePinFormat(newPin)) {
    return res.status(400).json({ ok: false, error: "invalid_pin" });
  }

  st.vault.pin_hash = hashPin(newPin);
  st.vault.pin_set = true;
  st.vault.locked = true;
  st.vault.session_token = null;
  st.vault.session_expires_utc = null;
  pushEvent(st, "vault", "PIN changed");
  saveState(st);
  return res.json({ ok: true });
}

app.post("/vault/pin/set", handleSetPin);
app.post("/vault/pin", handleSetPin);

// Keep your legacy endpoint too (alias)
app.post("/vault/set-pin", handleSetPin);

// Unlock endpoints
function handleUnlock(req, res) {
  if (!vaultEnabledOr403(res)) return;

  const st = loadState();
  const pin = String(req.body?.pin ?? req.body?.current_pin ?? "");

  if (!st.vault.pin_set || !st.vault.pin_hash) {
    return res.status(400).json({ ok: false, error: "pin_not_set" });
  }

  if (!validatePinFormat(pin) || hashPin(pin) !== st.vault.pin_hash) {
    return res.status(401).json({ ok: false, error: "bad_pin" });
  }

  st.vault.locked = false;
  const session = makeSession(st, 30);
  pushEvent(st, "vault", "Vault unlocked");
  saveState(st);

  res.json({ ok: true, ...session });
}

app.post("/vault/unlock", handleUnlock);
app.post("/vault/pin/use", handleUnlock);
app.post("/vault/use-pin", handleUnlock);

app.post("/vault/lock", (_req, res) => {
  const st = loadState();
  st.vault.locked = true;
  st.vault.session_token = null;
  st.vault.session_expires_utc = null;
  pushEvent(st, "vault", "Vault locked");
  saveState(st);
  res.json({ ok: true });
});

// ---------- Start ----------
app.listen(PORT, "0.0.0.0", () => {
  ensureDir();
  console.log(`crypto-ai-api listening on :${PORT}`);
});

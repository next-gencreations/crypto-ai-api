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

// Vault session duration (minutes)
const VAULT_SESSION_MINUTES = Number(process.env.VAULT_SESSION_MINUTES || "30");

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
  // Simple stable hash (upgrade later if desired)
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

function sessionTtlSeconds(st) {
  const exp = st?.vault?.session_expires_utc;
  if (!exp) return 0;
  const ms = new Date(exp).getTime() - Date.now();
  return ms > 0 ? Math.floor(ms / 1000) : 0;
}

function refreshAutoLockIfExpired(st) {
  // Auto-lock only if TTL has expired
  const ttl = sessionTtlSeconds(st);
  if (st.vault.locked) return;
  if (ttl <= 0) {
    st.vault.locked = true;
    st.vault.session_token = null;
    st.vault.session_expires_utc = null;
    pushEvent(st, "vault", "Session expired, vault locked");
  }
}

function makeSession(st) {
  const token = crypto.randomBytes(16).toString("hex");
  const expires = new Date(Date.now() + VAULT_SESSION_MINUTES * 60 * 1000).toISOString();
  st.vault.session_token = token;
  st.vault.session_expires_utc = expires;
  st.vault.locked = false;
  return { token, expires_utc: expires };
}

function getClientToken(req) {
  const h = req.headers["x-vault-token"];
  if (typeof h === "string" && h.trim()) return h.trim();
  // also allow body token field if used anywhere
  const b = req.body?.token;
  if (typeof b === "string" && b.trim()) return b.trim();
  return null;
}

function tokenValid(st, token) {
  if (!token) return false;
  if (!st.vault.session_token) return false;
  if (token !== st.vault.session_token) return false;
  return sessionTtlSeconds(st) > 0;
}

// ---------- Middleware ----------
app.use(express.json({ limit: "1mb" }));

app.use(
  cors({
    origin: CORS_ORIGIN === "*" ? true : CORS_ORIGIN,
    credentials: true,
    allowedHeaders: ["Content-Type", "X-Vault-Token", "Authorization"],
    methods: ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
  })
);

// Handle OPTIONS for everything (helps browser + Vercel proxy)
app.options("*", (_req, res) => {
  res.status(204).send();
});

// ---------- Routes ----------

// Root
app.get("/", (_req, res) => {
  res.json({ ok: true, service: "crypto-ai-api" });
});

// Health
app.get("/health", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();
  refreshAutoLockIfExpired(st);
  saveState(st);

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
    last_heartbeat_utc: st.system.last_heartbeat_utc,
  });
});

// Bankroll get/update
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

// Data (dashboard uses this)
app.get("/data", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();
  refreshAutoLockIfExpired(st);
  saveState(st);

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
      session_ttl_seconds: vaultEnabled ? sessionTtlSeconds(st) : 0,
    },
  });
});

// Logs
app.get("/logs", (_req, res) => {
  const st = loadState();
  res.json({
    ok: true,
    lines: (st.events || [])
      .slice(-120)
      .map((e) => `${e.time_utc} | ${e.type} | ${e.message}`),
  });
});

// OHLC placeholder
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

// ---------- Vault (IMPORTANT) ----------
// The dashboard is calling /vault/status, /vault/pin/set, /vault/unlock, /vault/lock.
// We also support legacy routes to prevent “Cannot POST /vault/pin”.

function vaultEnabledOr403(req, res) {
  const settings = loadSettings();
  const enabled = settings.vault_enabled !== false;
  if (!enabled) {
    res.status(403).json({ ok: false, error: "vault_disabled" });
    return false;
  }
  return true;
}

app.get("/vault/status", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();
  refreshAutoLockIfExpired(st);
  saveState(st);

  const enabled = settings.vault_enabled !== false;
  const locked = enabled ? !!st.vault.locked : true;

  res.json({
    ok: true,
    enabled,
    pin_set: !!st.vault.pin_set,
    locked,
    session_ttl_seconds: enabled ? sessionTtlSeconds(st) : 0,
    // token is optional; UI may store it
    token: st.vault.session_token || null,
  });
});

/**
 * SET PIN
 * Supports:
 * - Initial set: { new_pin } OR { pin, new_pin } (new_pin used)
 * - Change pin: { pin, new_pin }  (pin must be correct)
 */
async function handleSetPin(req, res) {
  if (!vaultEnabledOr403(req, res)) return;

  const st = loadState();
  refreshAutoLockIfExpired(st);

  const pin = String(req.body?.pin ?? req.body?.current_pin ?? "");
  const newPin = String(req.body?.new_pin ?? req.body?.newPin ?? req.body?.set_pin ?? req.body?.new ?? "");

  const candidate = newPin || pin; // allow {pin: "1234"} as first-time set
  if (!candidate || candidate.length < 4 || candidate.length > 12) {
    return res.status(400).json({ ok: false, error: "invalid_pin" });
  }

  // If pin already set, require current pin to change it
  if (st.vault.pin_set && st.vault.pin_hash) {
    if (!pin) return res.status(400).json({ ok: false, error: "current_pin_required" });
    if (hashPin(pin) !== st.vault.pin_hash) {
      return res.status(401).json({ ok: false, error: "bad_pin" });
    }
  }

  st.vault.pin_hash = hashPin(candidate);
  st.vault.pin_set = true;

  // setting/changing a pin locks vault and clears session
  st.vault.locked = true;
  st.vault.session_token = null;
  st.vault.session_expires_utc = null;

  pushEvent(st, "vault", st.vault.pin_set ? "PIN set/updated" : "PIN set");
  saveState(st);

  res.json({ ok: true });
}

// Modern endpoints
app.post("/vault/pin/set", handleSetPin);

// Legacy aliases (cover older UI / earlier attempts)
app.post("/vault/set-pin", handleSetPin);
app.post("/vault/pin", handleSetPin);
app.post("/vault/pin/set-pin", handleSetPin);

/**
 * UNLOCK
 * body: { pin }
 */
async function handleUnlock(req, res) {
  if (!vaultEnabledOr403(req, res)) return;

  const st = loadState();
  refreshAutoLockIfExpired(st);

  if (!st.vault.pin_set || !st.vault.pin_hash) {
    return res.status(400).json({ ok: false, error: "pin_not_set" });
  }

  const pin = String(req.body?.pin ?? "");
  if (!pin) return res.status(400).json({ ok: false, error: "pin_required" });

  if (hashPin(pin) !== st.vault.pin_hash) {
    return res.status(401).json({ ok: false, error: "bad_pin" });
  }

  const session = makeSession(st);
  pushEvent(st, "vault", "Vault unlocked");
  saveState(st);

  res.json({ ok: true, ...session, session_ttl_seconds: sessionTtlSeconds(st) });
}

app.post("/vault/unlock", handleUnlock);

// Legacy alias
app.post("/vault/use-pin", handleUnlock);
app.post("/vault/pin/use", handleUnlock);

/**
 * LOCK
 * Optional: If token provided, we accept it; also allow lock without token.
 */
app.post("/vault/lock", (req, res) => {
  if (!vaultEnabledOr403(req, res)) return;

  const st = loadState();

  // If token is supplied, validate it (prevents random external lock attempts)
  const token = getClientToken(req);
  if (token && !tokenValid(st, token)) {
    return res.status(401).json({ ok: false, error: "bad_token" });
  }

  st.vault.locked = true;
  st.vault.session_token = null;
  st.vault.session_expires_utc = null;

  pushEvent(st, "vault", "Vault locked");
  saveState(st);

  res.json({ ok: true });
});

// Optional: keepalive endpoint (refresh TTL without unlocking again)
app.post("/vault/keepalive", (req, res) => {
  if (!vaultEnabledOr403(req, res)) return;
  const st = loadState();
  refreshAutoLockIfExpired(st);

  const token = getClientToken(req);
  if (!tokenValid(st, token)) {
    return res.status(401).json({ ok: false, error: "bad_token" });
  }

  // Extend TTL
  const expires = new Date(Date.now() + VAULT_SESSION_MINUTES * 60 * 1000).toISOString();
  st.vault.session_expires_utc = expires;
  saveState(st);

  res.json({ ok: true, expires_utc: expires, session_ttl_seconds: sessionTtlSeconds(st) });
});

// ---------- Start ----------
app.listen(PORT, "0.0.0.0", () => {
  ensureDir();
  console.log(`crypto-ai-api listening on :${PORT}`);
});

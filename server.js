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
const SETTINGS_FILE =
  process.env.SETTINGS_PATH || path.join(DATA_DIR, "settings.json");
const STATE_FILE = process.env.STATE_PATH || path.join(DATA_DIR, "state.json");

const GBPUSD_RATE = Number(process.env.GBPUSD_RATE || "1.27");

// CORS
const CORS_ORIGIN = process.env.CORS_ORIGIN || "*";

// Vault session TTL (minutes)
const VAULT_SESSION_MINUTES = Number(process.env.VAULT_SESSION_MINUTES || "30");

// Optional salt (recommended)
const VAULT_SALT = process.env.VAULT_SALT || "change-me-in-render-env";

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

function safeEq(a, b) {
  try {
    const ba = Buffer.from(String(a));
    const bb = Buffer.from(String(b));
    if (ba.length !== bb.length) return false;
    return crypto.timingSafeEqual(ba, bb);
  } catch {
    return false;
  }
}

// hashed pin with salt
function hashPin(pin) {
  return crypto
    .createHash("sha256")
    .update(`${VAULT_SALT}:${String(pin)}`)
    .digest("hex");
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
    bankroll: { amount_gbp: 1000 },
    vault: {
      enabled: true,
      pin_set: false,
      locked: true,
      pin_hash: null,
      session_token: null,
      session_expires_utc: null,
      failed_attempts: 0,
      lockout_until_utc: null,
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

// If session expired -> relock
function normalizeVaultSession(st, settings) {
  const enabled = settings.vault_enabled !== false;

  // Vault disabled = treat as locked
  if (!enabled) {
    st.vault.locked = true;
    st.vault.session_token = null;
    st.vault.session_expires_utc = null;
    return { enabled: false, locked: true };
  }

  // If expiry passed, relock
  if (st.vault.session_expires_utc) {
    const exp = Date.parse(st.vault.session_expires_utc);
    if (Number.isFinite(exp) && Date.now() > exp) {
      st.vault.locked = true;
      st.vault.session_token = null;
      st.vault.session_expires_utc = null;
      pushEvent(st, "vault", "Vault session expired -> locked");
      saveState(st);
    }
  }

  return { enabled: true, locked: !!st.vault.locked };
}

// simple lockout logic for PIN brute force
function isLockedOut(st) {
  if (!st.vault.lockout_until_utc) return false;
  const t = Date.parse(st.vault.lockout_until_utc);
  return Number.isFinite(t) && Date.now() < t;
}

function setLockout(st, minutes) {
  st.vault.lockout_until_utc = new Date(Date.now() + minutes * 60 * 1000).toISOString();
}

function clearLockout(st) {
  st.vault.failed_attempts = 0;
  st.vault.lockout_until_utc = null;
}

function issueSession(st) {
  st.vault.locked = false;
  st.vault.session_token = crypto.randomBytes(16).toString("hex");
  st.vault.session_expires_utc = new Date(
    Date.now() + VAULT_SESSION_MINUTES * 60 * 1000
  ).toISOString();
}

function getVaultTokenFromReq(req) {
  const h =
    req.headers["x-vault-token"] ||
    req.headers["x-vault-token".toLowerCase()] ||
    req.headers["authorization"];
  if (!h) return null;
  // accept "Bearer xxx" or plain token
  const s = String(h);
  if (s.toLowerCase().startsWith("bearer ")) return s.slice(7).trim();
  return s.trim();
}

function requireVaultEnabled(settings, res) {
  if (settings.vault_enabled === false) {
    res.status(403).json({ ok: false, error: "vault_disabled" });
    return false;
  }
  return true;
}

// ---------- Middleware ----------
app.use(express.json({ limit: "1mb" }));
app.use(
  cors({
    origin: CORS_ORIGIN === "*" ? true : CORS_ORIGIN,
    credentials: true,
  })
);

// ---------- Routes ----------
app.get("/", (_req, res) => {
  res.json({ ok: true, service: "crypto-ai-api" });
});

// Health
app.get("/health", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();
  const v = normalizeVaultSession(st, settings);

  res.json({
    ok: true,
    status: 200,
    service: "crypto-ai-api",
    time_utc: nowUtc(),
    vault_enabled: v.enabled,
    vault_unlocked: v.enabled ? !v.locked : false,
  });
});

// Settings
app.get("/settings", (_req, res) => res.json(loadSettings()));

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

// Data
app.get("/data", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();
  const v = normalizeVaultSession(st, settings);

  const bankroll = st.bankroll?.amount_gbp ?? 1000;
  const equityUsd = (Number(bankroll) * GBPUSD_RATE).toFixed(2);

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
      enabled: v.enabled,
      pin_set: !!st.vault.pin_set,
      locked: v.enabled ? !!st.vault.locked : true,
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
  const v = normalizeVaultSession(st, settings);

  res.json({
    ok: true,
    enabled: v.enabled,
    pin_set: !!st.vault.pin_set,
    locked: v.enabled ? !!st.vault.locked : true,
  });
});

/**
 * IMPORTANT:
 * Your UI / proxy has tried:
 * - GET /vault/pin
 * - POST /vault/pin
 * - POST /vault/pin/set
 * - POST /vault/pin/set (legacy)
 *
 * So we implement ALL of them as aliases.
 */

// ✅ Stop "Cannot GET /vault/pin"
app.get("/vault/pin", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();
  const v = normalizeVaultSession(st, settings);
  res.json({
    ok: true,
    enabled: v.enabled,
    pin_set: !!st.vault.pin_set,
    locked: v.enabled ? !!st.vault.locked : true,
  });
});

// Legacy: /vault/set-pin  (kept)
app.post("/vault/set-pin", (req, res) => {
  const settings = loadSettings();
  if (!requireVaultEnabled(settings, res)) return;

  const pin = String(req.body?.pin ?? req.body?.new_pin ?? req.body?.newPin ?? "");
  if (pin.length < 4 || pin.length > 8) {
    return res.status(400).json({ ok: false, error: "invalid_pin" });
  }

  const st = loadState();
  st.vault.pin_hash = hashPin(pin);
  st.vault.pin_set = true;
  st.vault.locked = true;
  st.vault.session_token = null;
  st.vault.session_expires_utc = null;
  clearLockout(st);

  pushEvent(st, "vault", "PIN set");
  saveState(st);
  res.json({ ok: true });
});

// ✅ New: /vault/pin/set  (what your dashboard expects)
app.post("/vault/pin/set", (req, res) => {
  // just call the same handler as /vault/set-pin
  req.url = "/vault/set-pin";
  return app._router.handle(req, res);
});

// Legacy: /vault/use-pin (kept)
app.post("/vault/use-pin", (req, res) => {
  const settings = loadSettings();
  if (!requireVaultEnabled(settings, res)) return;

  const pin = String(req.body?.pin ?? req.body?.current_pin ?? req.body?.old_pin ?? "");
  const st = loadState();
  normalizeVaultSession(st, settings);

  if (isLockedOut(st)) {
    return res.status(429).json({ ok: false, error: "locked_out" });
  }

  if (!st.vault.pin_set || !st.vault.pin_hash) {
    return res.status(400).json({ ok: false, error: "pin_not_set" });
  }

  const ok = safeEq(hashPin(pin), st.vault.pin_hash);
  if (!ok) {
    st.vault.failed_attempts = (st.vault.failed_attempts || 0) + 1;
    pushEvent(st, "vault", "Bad PIN attempt");

    // lockout after 5 tries for 5 minutes
    if (st.vault.failed_attempts >= 5) {
      setLockout(st, 5);
      pushEvent(st, "vault", "PIN lockout 5 minutes");
    }

    saveState(st);
    return res.status(401).json({ ok: false, error: "bad_pin" });
  }

  clearLockout(st);
  issueSession(st);
  pushEvent(st, "vault", "Vault unlocked");
  saveState(st);

  res.json({
    ok: true,
    token: st.vault.session_token,
    expires_utc: st.vault.session_expires_utc,
  });
});

// ✅ New: /vault/pin/use (alias)
app.post("/vault/pin/use", (req, res) => {
  req.url = "/vault/use-pin";
  return app._router.handle(req, res);
});

// ✅ New: /vault/unlock (alias)
app.post("/vault/unlock", (req, res) => {
  req.url = "/vault/use-pin";
  return app._router.handle(req, res);
});

// ✅ Optional: Validate current session
app.get("/vault/session", (req, res) => {
  const settings = loadSettings();
  const st = loadState();
  const v = normalizeVaultSession(st, settings);

  if (!v.enabled) {
    return res.json({ ok: true, enabled: false, locked: true });
  }

  const token = getVaultTokenFromReq(req);
  const valid =
    token &&
    st.vault.session_token &&
    safeEq(String(token), String(st.vault.session_token)) &&
    !st.vault.locked;

  res.json({
    ok: true,
    enabled: true,
    locked: !!st.vault.locked,
    valid: !!valid,
    expires_utc: st.vault.session_expires_utc,
  });
});

// ✅ New: /vault/pin (smart endpoint)
// If body has new_pin/newPin -> set/change PIN
// Else -> unlock using pin
app.post("/vault/pin", (req, res) => {
  const settings = loadSettings();
  if (!requireVaultEnabled(settings, res)) return;

  const st = loadState();
  normalizeVaultSession(st, settings);

  const currentPin = String(
    req.body?.pin ??
      req.body?.current_pin ??
      req.body?.old_pin ??
      req.body?.currentPin ??
      ""
  );
  const newPin = String(req.body?.new_pin ?? req.body?.newPin ?? "");

  // If newPin supplied -> set/change
  if (newPin) {
    if (newPin.length < 4 || newPin.length > 8) {
      return res.status(400).json({ ok: false, error: "invalid_pin" });
    }

    // If a PIN already exists, require current pin to change it
    if (st.vault.pin_set && st.vault.pin_hash) {
      if (!currentPin) {
        return res.status(400).json({ ok: false, error: "current_pin_required" });
      }
      const ok = safeEq(hashPin(currentPin), st.vault.pin_hash);
      if (!ok) {
        return res.status(401).json({ ok: false, error: "bad_pin" });
      }
      pushEvent(st, "vault", "PIN changed");
    } else {
      pushEvent(st, "vault", "PIN set");
    }

    st.vault.pin_hash = hashPin(newPin);
    st.vault.pin_set = true;
    st.vault.locked = true;
    st.vault.session_token = null;
    st.vault.session_expires_utc = null;
    clearLockout(st);

    saveState(st);
    return res.json({ ok: true });
  }

  // Otherwise treat as unlock
  req.url = "/vault/use-pin";
  return app._router.handle(req, res);
});

// Lock vault
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

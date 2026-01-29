import express from "express";
import cors from "cors";
import crypto from "crypto";
import fs from "fs";
import path from "path";
import jwt from "jsonwebtoken";

// Node 18+ has global fetch; Node 22 definitely does.
// If you ever run locally on older node, install node-fetch.

const app = express();

// ---------- ENV ----------
const PORT = Number(process.env.PORT || 10000);

// Persisted storage directory on Render
const DATA_DIR = process.env.DATA_DIR || "/var/data";
const SETTINGS_FILE = process.env.SETTINGS_PATH || path.join(DATA_DIR, "settings.json");
const STATE_FILE = process.env.STATE_PATH || path.join(DATA_DIR, "state.json");

// CORS
const CORS_ORIGIN = process.env.CORS_ORIGIN || "*";

// Coinbase
const COINBASE_HOST = "api.coinbase.com";
const COINBASE_BASE = `https://${COINBASE_HOST}`;
const COINBASE_ACCOUNTS_PATH = "/api/v3/brokerage/accounts";

// Vault master (used to encrypt keys at rest)
const VAULT_MASTER_KEY = process.env.VAULT_MASTER_KEY || process.env.VAULT_KEY || "";
const VAULT_TTL_SECONDS = Number(process.env.VAULT_TTL_SECONDS || "1800");

// Logs
const MAX_LOG_LINES = Number(process.env.MAX_LOG_LINES || "600");

// ---------- Helpers ----------
function ensureDir() {
  fs.mkdirSync(DATA_DIR, { recursive: true });
}

function readJson(file, fallback) {
  try {
    if (!fs.existsSync(file)) return fallback;
    return JSON.parse(fs.readFileSync(file, "utf-8"));
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

function defaultSettings() {
  return {
    vault_enabled: true,
    build_tag: process.env.NEXT_PUBLIC_BUILD_TAG || "v1",
    bankroll_gbp: 1000,
    companion_name: "Vault Girl",
    markets: ["BTCUSDT", "ETHUSDT"],
  };
}

function defaultState() {
  return {
    vault: {
      pin_set: false,
      locked: true,
      pin_hash: null,
      session_token: null,
      session_expires_utc: null,
    },

    vault_keys: [], // [{id,name,exchange,enc,created_utc}]

    // Dashboard/bot state
    pet: {
      name: "Vault Girl",
      stage: "cryo", // cryo | awake | ...
      mood: "idle",
      health: 100,
      hunger: 100,
      growth: 0,
      updated_utc: null,
    },

    bot: {
      last_heartbeat_utc: null,
      open_positions: 0,
      survival: "cryo",
      equity: { usd: 0 },
    },

    logs: [], // [{t, level, msg, extra?}]
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
  return s && typeof s === "object" ? { ...defaultState(), ...s } : defaultState();
}
function saveState(st) {
  writeJson(STATE_FILE, st);
}

function logLine(level, msg, extra = null) {
  const st = loadState();
  st.logs = Array.isArray(st.logs) ? st.logs : [];
  st.logs.push({ t: nowUtc(), level, msg, extra });
  if (st.logs.length > MAX_LOG_LINES) st.logs = st.logs.slice(st.logs.length - MAX_LOG_LINES);
  saveState(st);
}

// ---------- Crypto helpers ----------
function sha256Hex(s) {
  return crypto.createHash("sha256").update(String(s)).digest("hex");
}
function hashPin(pin) {
  return sha256Hex(pin);
}

// --- AES-256-GCM encryption for key material ---
function requireVaultMasterKey() {
  if (!VAULT_MASTER_KEY || VAULT_MASTER_KEY.length < 16) {
    throw new Error("VAULT_MASTER_KEY missing/too short. Set it in Render env vars.");
  }
}
function keyBytes() {
  return crypto.createHash("sha256").update(VAULT_MASTER_KEY).digest(); // 32 bytes
}
function aesGcmEncrypt(plaintext) {
  requireVaultMasterKey();
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", keyBytes(), iv);
  const enc = Buffer.concat([cipher.update(String(plaintext), "utf8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  return {
    iv: iv.toString("base64"),
    tag: tag.toString("base64"),
    data: enc.toString("base64"),
    alg: "aes-256-gcm",
  };
}
function aesGcmDecrypt(encObj) {
  requireVaultMasterKey();
  const iv = Buffer.from(encObj.iv, "base64");
  const tag = Buffer.from(encObj.tag, "base64");
  const data = Buffer.from(encObj.data, "base64");
  const decipher = crypto.createDecipheriv("aes-256-gcm", keyBytes(), iv);
  decipher.setAuthTag(tag);
  const out = Buffer.concat([decipher.update(data), decipher.final()]);
  return out.toString("utf8");
}

function isVaultUnlocked(st) {
  if (!st?.vault?.session_token) return false;
  if (!st?.vault?.session_expires_utc) return false;
  return new Date(st.vault.session_expires_utc).getTime() > Date.now();
}

function requireVaultToken(req, st) {
  const token = req.headers["x-vault-token"];
  if (!token) return { ok: false, status: 401, error: "missing_vault_token" };
  if (!isVaultUnlocked(st)) return { ok: false, status: 401, error: "vault_locked" };
  if (String(token) !== String(st.vault.session_token))
    return { ok: false, status: 401, error: "bad_vault_token" };
  return { ok: true };
}

// Coinbase JWT generation
function buildCoinbaseRestJwt(keyName, privateKeyPem, method, requestPath) {
  const now = Math.floor(Date.now() / 1000);
  const uri = `${method.toUpperCase()} ${COINBASE_HOST}${requestPath}`;

  const payload = {
    sub: keyName,
    iss: "cdp",
    nbf: now,
    exp: now + 120,
    uri,
    iat: now,
  };

  const header = {
    kid: keyName,
    nonce: crypto.randomBytes(16).toString("hex"),
    typ: "JWT",
  };

  // Normalize \n sequences into real newlines
  const pem = String(privateKeyPem).includes("\\n")
    ? String(privateKeyPem).replace(/\\n/g, "\n")
    : String(privateKeyPem);

  return jwt.sign(payload, pem, { algorithm: "ES256", header });
}

function findVaultKey(st, { name, id }) {
  if (!st?.vault_keys?.length) return null;
  if (name) return st.vault_keys.find((k) => k.name === name) || null;
  if (id) return st.vault_keys.find((k) => k.id === id) || null;
  return null;
}

// Prefer newest key if multiple share same name
function findNewestKeyByName(st, name) {
  const keys = (st.vault_keys || []).filter((k) => k.name === name);
  if (!keys.length) return null;
  keys.sort((a, b) => new Date(b.created_utc).getTime() - new Date(a.created_utc).getTime());
  return keys[0];
}

// ---------- Middleware ----------
app.use(express.json({ limit: "2mb" }));
app.use(
  cors({
    origin: CORS_ORIGIN === "*" ? true : CORS_ORIGIN,
    credentials: true,
  })
);

// ---------- Basic ----------
app.get("/", (_req, res) => {
  res.json({ ok: true, service: "crypto-ai-api", time_utc: nowUtc() });
});

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
    vault_unlocked: vaultEnabled ? isVaultUnlocked(st) : false,
  });
});

// ---------- Settings ----------
app.get("/settings", (_req, res) => res.json(loadSettings()));

app.post("/settings", (req, res) => {
  const current = loadSettings();
  const patch = req.body && typeof req.body === "object" ? req.body : {};
  const merged = { ...current, ...patch };
  saveSettings(merged);

  // keep pet name synced if companion_name changes
  const st = loadState();
  if (merged.companion_name && st.pet) {
    st.pet.name = String(merged.companion_name);
    saveState(st);
  }

  logLine("info", "settings_updated", { patch });
  res.json({ ok: true, settings: merged });
});

// ---------- Logs ----------
app.get("/logs", (_req, res) => {
  const st = loadState();
  res.json({ ok: true, lines: st.logs || [] });
});

// ---------- Data (dashboard expects this) ----------
app.get("/data", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();

  res.json({
    ok: true,
    time_utc: nowUtc(),
    markets: settings.markets || ["BTCUSDT", "ETHUSDT"],
    open_positions: st.bot?.open_positions ?? 0,
    survival: st.bot?.survival ?? (st.pet?.stage || "cryo"),
    last_heartbeat_utc: st.bot?.last_heartbeat_utc,
    equity: st.bot?.equity || { usd: 0 },
    settings: {
      bankroll_gbp: settings.bankroll_gbp ?? 1000,
      companion_name: settings.companion_name ?? "Vault Girl",
    },
    pet: st.pet || defaultState().pet,
  });
});

// ---------- Pet ----------
app.get("/pet", (_req, res) => {
  const st = loadState();
  res.json({ ok: true, pet: st.pet || defaultState().pet });
});

app.post("/pet", (req, res) => {
  const st = loadState();
  st.pet = st.pet || defaultState().pet;

  const patch = req.body && typeof req.body === "object" ? req.body : {};
  const safeNumber = (v, d) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : d;
  };

  if (typeof patch.stage === "string") st.pet.stage = patch.stage;
  if (typeof patch.mood === "string") st.pet.mood = patch.mood;
  if (typeof patch.name === "string") st.pet.name = patch.name;

  if (patch.health != null) st.pet.health = Math.max(0, Math.min(100, safeNumber(patch.health, st.pet.health)));
  if (patch.hunger != null) st.pet.hunger = Math.max(0, Math.min(100, safeNumber(patch.hunger, st.pet.hunger)));
  if (patch.growth != null) st.pet.growth = Math.max(0, safeNumber(patch.growth, st.pet.growth));

  st.pet.updated_utc = nowUtc();
  saveState(st);

  logLine("info", "pet_updated", { pet: st.pet });
  res.json({ ok: true, pet: st.pet });
});

// ---------- Bot heartbeat (runner posts here) ----------
app.post("/bot/heartbeat", (req, res) => {
  const st = loadState();
  st.bot = st.bot || defaultState().bot;

  const body = req.body && typeof req.body === "object" ? req.body : {};

  if (body.open_positions != null) st.bot.open_positions = Number(body.open_positions) || 0;
  if (body.survival != null) st.bot.survival = String(body.survival);
  if (body.equity && typeof body.equity === "object") st.bot.equity = body.equity;

  st.bot.last_heartbeat_utc = nowUtc();
  saveState(st);

  logLine("info", "bot_heartbeat", { open_positions: st.bot.open_positions, equity: st.bot.equity });
  res.json({ ok: true, time_utc: st.bot.last_heartbeat_utc });
});

// ---------- Vault ----------
app.get("/vault/status", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();
  const enabled = settings.vault_enabled !== false;
  const unlocked = enabled ? isVaultUnlocked(st) : false;

  const ttl = unlocked
    ? Math.max(0, Math.floor((new Date(st.vault.session_expires_utc).getTime() - Date.now()) / 1000))
    : 0;

  res.json({ ok: true, enabled, pin_set: !!st.vault.pin_set, unlocked, ttl_sec: ttl });
});

app.post("/vault/set-pin", (req, res) => {
  const settings = loadSettings();
  if (settings.vault_enabled === false) return res.status(403).json({ ok: false, error: "vault_disabled" });

  const pin = String(req.body?.pin || "");
  if (pin.length < 4 || pin.length > 8) return res.status(400).json({ ok: false, error: "invalid_pin" });

  const st = loadState();
  st.vault.pin_hash = hashPin(pin);
  st.vault.pin_set = true;
  st.vault.locked = true;
  st.vault.session_token = null;
  st.vault.session_expires_utc = null;
  saveState(st);

  logLine("info", "pin_set");
  res.json({ ok: true });
});

app.post("/vault/unlock", (req, res) => {
  const settings = loadSettings();
  if (settings.vault_enabled === false) return res.status(403).json({ ok: false, error: "vault_disabled" });

  const pin = String(req.body?.pin || "");
  const st = loadState();

  if (!st.vault.pin_set || !st.vault.pin_hash) return res.status(400).json({ ok: false, error: "pin_not_set" });
  if (hashPin(pin) !== st.vault.pin_hash) return res.status(401).json({ ok: false, error: "bad_pin" });

  st.vault.locked = false;
  st.vault.session_token = crypto.randomBytes(16).toString("hex");
  st.vault.session_expires_utc = new Date(Date.now() + VAULT_TTL_SECONDS * 1000).toISOString();
  saveState(st);

  logLine("info", "vault_unlocked", { ttl_sec: VAULT_TTL_SECONDS });

  res.json({
    ok: true,
    token: st.vault.session_token,
    ttl_sec: VAULT_TTL_SECONDS,
    expires_utc: st.vault.session_expires_utc,
  });
});

app.post("/vault/lock", (_req, res) => {
  const st = loadState();
  st.vault.locked = true;
  st.vault.session_token = null;
  st.vault.session_expires_utc = null;
  saveState(st);
  logLine("info", "vault_locked");
  res.json({ ok: true });
});

// Store a key
app.post("/vault/keys", (req, res) => {
  const st = loadState();
  const chk = requireVaultToken(req, st);
  if (!chk.ok) return res.status(chk.status).json({ ok: false, error: chk.error });

  const name = String(req.body?.name || req.body?.label || "").trim();
  const exchange = String(req.body?.exchange || "").trim().toLowerCase();
  const api_key = String(req.body?.api_key || "").trim();
  const api_secret = String(req.body?.api_secret || "");

  if (!/^[a-z0-9_-]{3,64}$/i.test(name)) return res.status(400).json({ ok: false, error: "bad_name" });
  if (!exchange) return res.status(400).json({ ok: false, error: "bad_exchange" });
  if (!api_key) return res.status(400).json({ ok: false, error: "missing_api_key" });
  if (!api_secret) return res.status(400).json({ ok: false, error: "missing_api_secret" });

  if (exchange === "coinbase") {
    if (!api_key.startsWith("organizations/") || !api_key.includes("/apiKeys/")) {
      return res.status(400).json({ ok: false, error: "bad_coinbase_key_name" });
    }
  }

  const id = crypto.randomBytes(8).toString("hex");
  const enc = aesGcmEncrypt(JSON.stringify({ api_key, api_secret }));

  const entry = { id, name, exchange, enc, created_utc: nowUtc() };
  st.vault_keys = Array.isArray(st.vault_keys) ? st.vault_keys : [];
  st.vault_keys.push(entry);
  saveState(st);

  logLine("info", "vault_key_added", { id, name, exchange });

  res.json({
    ok: true,
    id,
    name,
    created_utc: entry.created_utc,
    ttl_sec: Math.max(0, Math.floor((new Date(st.vault.session_expires_utc).getTime() - Date.now()) / 1000)),
    encryption: "aes-256-gcm",
  });
});

// List keys (no secrets)
app.get("/vault/keys", (req, res) => {
  const st = loadState();
  const chk = requireVaultToken(req, st);
  if (!chk.ok) return res.status(chk.status).json({ ok: false, error: chk.error });

  res.json({
    ok: true,
    keys: (st.vault_keys || []).map((k) => ({ id: k.id, name: k.name, created_utc: k.created_utc })),
    ttl_sec: Math.max(0, Math.floor((new Date(st.vault.session_expires_utc).getTime() - Date.now()) / 1000)),
    encryption: "aes-256-gcm",
  });
});

// ---------- Coinbase ----------
app.get("/coinbase/ping", async (req, res) => {
  const st = loadState();
  const chk = requireVaultToken(req, st);
  if (!chk.ok) return res.status(chk.status).json({ ok: false, error: chk.error });

  const entry = findNewestKeyByName(st, "coinbase_main");
  if (!entry) return res.status(404).json({ ok: false, error: "coinbase_key_not_found" });

  const { api_key, api_secret } = JSON.parse(aesGcmDecrypt(entry.enc));
  const token = buildCoinbaseRestJwt(api_key, api_secret, "GET", COINBASE_ACCOUNTS_PATH);

  try {
    const r = await fetch(COINBASE_BASE + COINBASE_ACCOUNTS_PATH, {
      method: "GET",
      headers: { Authorization: `Bearer ${token}` },
    });

    if (!r.ok) {
      const txt = await r.text();
      return res.status(502).json({
        ok: false,
        error: "coinbase_auth_failed",
        status: r.status,
        detail: txt.slice(0, 300),
      });
    }

    return res.json({ ok: true, coinbase: "authenticated", using_key_id: entry.id });
  } catch (e) {
    return res.status(502).json({ ok: false, error: "coinbase_fetch_failed", detail: String(e) });
  }
});

app.get("/coinbase/accounts", async (req, res) => {
  const st = loadState();
  const chk = requireVaultToken(req, st);
  if (!chk.ok) return res.status(chk.status).json({ ok: false, error: chk.error });

  const entry = findNewestKeyByName(st, "coinbase_main");
  if (!entry) return res.status(404).json({ ok: false, error: "coinbase_key_not_found" });

  const { api_key, api_secret } = JSON.parse(aesGcmDecrypt(entry.enc));
  const token = buildCoinbaseRestJwt(api_key, api_secret, "GET", COINBASE_ACCOUNTS_PATH);

  try {
    const r = await fetch(COINBASE_BASE + COINBASE_ACCOUNTS_PATH, {
      method: "GET",
      headers: { Authorization: `Bearer ${token}` },
    });

    const data = await r.json().catch(async () => ({ raw: await r.text() }));
    if (!r.ok) {
      return res.status(502).json({
        ok: false,
        error: "coinbase_accounts_failed",
        status: r.status,
        detail: data,
      });
    }

    return res.json({ ok: true, using_key_id: entry.id, data });
  } catch (e) {
    return res.status(502).json({ ok: false, error: "coinbase_fetch_failed", detail: String(e) });
  }
});

// ---------- OHLC (Binance klines for Candles page) ----------
// Example: /ohlc?market=BTCUSDT&interval=300&limit=600
app.get("/ohlc", async (req, res) => {
  const market = String(req.query.market || "BTCUSDT").toUpperCase();
  const intervalSec = Number(req.query.interval || "300");
  const limit = Math.min(1000, Math.max(10, Number(req.query.limit || "600")));

  // Binance interval mapping
  const map = {
    60: "1m",
    300: "5m",
    900: "15m",
    3600: "1h",
    14400: "4h",
    86400: "1d",
  };
  const interval = map[intervalSec] || "5m";

  try {
    const url =
      "https://api.binance.com/api/v3/klines?" +
      new URLSearchParams({ symbol: market, interval, limit: String(limit) }).toString();

    const r = await fetch(url, { method: "GET" });
    const raw = await r.json();

    if (!r.ok) {
      return res.status(502).json({ ok: false, error: "binance_ohlc_failed", status: r.status, detail: raw });
    }

    // Convert into {t,o,h,l,c,v}
    const candles = raw.map((k) => ({
      t: k[0],
      o: Number(k[1]),
      h: Number(k[2]),
      l: Number(k[3]),
      c: Number(k[4]),
      v: Number(k[5]),
    }));

    return res.json({ ok: true, market, interval_sec: intervalSec, candles });
  } catch (e) {
    return res.status(502).json({ ok: false, error: "binance_fetch_failed", detail: String(e) });
  }
});

// ---------- Start ----------
app.listen(PORT, "0.0.0.0", () => {
  ensureDir();
  logLine("info", "server_started", { port: PORT });
  console.log(`crypto-ai-api listening on :${PORT}`);
});

import express from "express";
import cors from "cors";
import crypto from "crypto";
import fs from "fs";
import path from "path";
import jwt from "jsonwebtoken";

// ---------------- App ----------------
const app = express();
const PORT = Number(process.env.PORT || 10000);

// Persisted storage directory on Render
const DATA_DIR = process.env.DATA_DIR || "/var/data";
const SETTINGS_FILE = process.env.SETTINGS_PATH || path.join(DATA_DIR, "settings.json");
const STATE_FILE = process.env.STATE_PATH || path.join(DATA_DIR, "state.json");

// CORS
const CORS_ORIGIN = process.env.CORS_ORIGIN || "*";

// Vault master (used to encrypt keys at rest)
const VAULT_MASTER_KEY = process.env.VAULT_MASTER_KEY || process.env.VAULT_KEY || "";
const VAULT_TTL_SECONDS = Number(process.env.VAULT_TTL_SECONDS || "1800");

// Coinbase
const COINBASE_HOST = "api.coinbase.com";
const COINBASE_BASE = `https://${COINBASE_HOST}`;
// Advanced Trade REST
const COINBASE_ACCOUNTS_PATH = "/api/v3/brokerage/accounts";

// Binance public (for chart candles)
const BINANCE_BASE = "https://api.binance.com";

// ---------------- Helpers ----------------
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
    bankroll_gbp: 1000,
    companion_name: "Vault Girl",
    build_tag: process.env.NEXT_PUBLIC_BUILD_TAG || "v1",
  };
}

function defaultState() {
  return {
    vault: {
      enabled: true,
      pin_set: false,
      locked: true,
      pin_hash: null,
      session_token: null,
      session_expires_utc: null,
    },
    vault_keys: [], // [{id,name,exchange,enc,created_utc}]
    bot: {
      markets: ["BTCUSDT", "ETHUSDT"],
      open_positions: 0,
      survival: "cryo",
      last_heartbeat_utc: null,
      equity: { usd: 0 },
      updated_utc: null,
    },
    pet: {
      name: "Vault Girl",
      stage: "cryo",
      mood: "idle",
      health: 100,
      hunger: 100,
      growth: 0,
      updated_utc: null,
    },
    logs: [], // [{t, level, msg, data?}]
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

function logLine(level, msg, data) {
  const st = loadState();
  st.logs = Array.isArray(st.logs) ? st.logs : [];
  st.logs.push({ t: nowUtc(), level, msg, data });
  // keep last 500
  if (st.logs.length > 500) st.logs = st.logs.slice(st.logs.length - 500);
  saveState(st);
}

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
  return { iv: iv.toString("base64"), tag: tag.toString("base64"), data: enc.toString("base64"), alg: "aes-256-gcm" };
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
  if (String(token) !== String(st.vault.session_token)) return { ok: false, status: 401, error: "bad_vault_token" };
  return { ok: true };
}

// Coinbase JWT (Advanced Trade REST)
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
  };

  // normalize if stored with \n sequences
  const pem = String(privateKeyPem).includes("\\n")
    ? String(privateKeyPem).replace(/\\n/g, "\n")
    : String(privateKeyPem);

  return jwt.sign(payload, pem, { algorithm: "ES256", header });
}

// Find newest key by name (handles duplicates)
function findNewestKeyByName(st, name) {
  const keys = Array.isArray(st?.vault_keys) ? st.vault_keys : [];
  const matches = keys.filter((k) => k.name === name);
  if (!matches.length) return null;
  matches.sort((a, b) => new Date(b.created_utc).getTime() - new Date(a.created_utc).getTime());
  return matches[0];
}

// ---------------- Middleware ----------------
app.use(express.json({ limit: "2mb" }));
app.use(
  cors({
    origin: CORS_ORIGIN === "*" ? true : CORS_ORIGIN,
    credentials: true,
  })
);

// ---------------- Basic ----------------
app.get("/", (_req, res) => res.json({ ok: true, service: "crypto-ai-api", time_utc: nowUtc() }));

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

// ---------------- Settings ----------------
app.get("/settings", (_req, res) => res.json(loadSettings()));

app.post("/settings", (req, res) => {
  const current = loadSettings();
  const patch = req.body && typeof req.body === "object" ? req.body : {};
  const merged = { ...current, ...patch };
  saveSettings(merged);

  // also keep pet name aligned
  const st = loadState();
  if (typeof merged.companion_name === "string" && merged.companion_name.trim()) {
    st.pet.name = merged.companion_name.trim();
    saveState(st);
  }

  logLine("info", "settings_updated", patch);
  res.json({ ok: true, settings: merged });
});

// ---------------- Data + Logs (for Dashboard) ----------------
app.get("/data", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();
  res.json({
    ok: true,
    time_utc: nowUtc(),
    markets: st.bot?.markets || ["BTCUSDT"],
    open_positions: st.bot?.open_positions ?? 0,
    survival: st.bot?.survival || "cryo",
    last_heartbeat_utc: st.bot?.last_heartbeat_utc || null,
    equity: st.bot?.equity || { usd: 0 },
    settings: {
      bankroll_gbp: settings.bankroll_gbp ?? 1000,
      companion_name: settings.companion_name ?? "Vault Girl",
    },
    pet: st.pet,
    bot: st.bot,
  });
});

app.get("/logs", (req, res) => {
  const st = loadState();
  const limit = Math.max(1, Math.min(500, Number(req.query.limit || "120")));
  const lines = (st.logs || []).slice(-limit);
  res.json({ ok: true, lines });
});

// ---------------- OHLC (candles for TradingView tab) ----------------
function secToBinanceInterval(sec) {
  const s = Number(sec);
  if (s <= 60) return "1m";
  if (s <= 180) return "3m";
  if (s <= 300) return "5m";
  if (s <= 900) return "15m";
  if (s <= 1800) return "30m";
  if (s <= 3600) return "1h";
  if (s <= 14400) return "4h";
  if (s <= 86400) return "1d";
  return "1m";
}

app.get("/ohlc", async (req, res) => {
  const market = String(req.query.market || "BTCUSDT").toUpperCase();
  const intervalSec = Number(req.query.interval || "60");
  const limit = Math.max(1, Math.min(1000, Number(req.query.limit || "600")));
  const interval = secToBinanceInterval(intervalSec);

  try {
    const url = `${BINANCE_BASE}/api/v3/klines?symbol=${encodeURIComponent(market)}&interval=${interval}&limit=${limit}`;
    const r = await fetch(url);
    if (!r.ok) {
      const txt = await r.text();
      return res.status(502).json({ ok: false, error: "binance_failed", status: r.status, detail: txt.slice(0, 200) });
    }
    const k = await r.json();
    // Map into common OHLC objects
    const out = k.map((row) => ({
      t: row[0], // open time ms
      o: Number(row[1]),
      h: Number(row[2]),
      l: Number(row[3]),
      c: Number(row[4]),
      v: Number(row[5]),
    }));
    res.json({ ok: true, market, interval, limit, candles: out });
  } catch (e) {
    res.status(502).json({ ok: false, error: "ohlc_fetch_failed", detail: String(e) });
  }
});

// ---------------- Bot ingest/control ----------------
// Bot heartbeat: updates what the dashboard shows
app.post("/ingest/heartbeat", (req, res) => {
  const st = loadState();
  const body = req.body && typeof req.body === "object" ? req.body : {};

  // Accept either style of fields
  const markets = Array.isArray(body.markets) ? body.markets : st.bot.markets;
  const open_positions = Number.isFinite(Number(body.open_positions)) ? Number(body.open_positions) : st.bot.open_positions;
  const equityUsd =
    body.equity?.usd != null ? Number(body.equity.usd) : body.equity_usd != null ? Number(body.equity_usd) : st.bot.equity.usd;
  const survival = typeof body.survival === "string" ? body.survival : st.bot.survival;

  st.bot.markets = markets;
  st.bot.open_positions = open_positions;
  st.bot.equity = { usd: Number.isFinite(equityUsd) ? equityUsd : st.bot.equity.usd };
  st.bot.survival = survival;
  st.bot.last_heartbeat_utc = nowUtc();
  st.bot.updated_utc = nowUtc();

  saveState(st);
  logLine("info", "heartbeat", { open_positions, equityUsd, survival });

  res.json({ ok: true, stored: true, time_utc: nowUtc() });
});

// Bot trade event (paper trade result)
app.post("/ingest/trade", (req, res) => {
  const st = loadState();
  const b = req.body && typeof req.body === "object" ? req.body : {};

  const pnl = Number(b.pnl ?? b.realized_pnl ?? 0);
  const win = pnl > 0;

  // Simple “pet reaction” shaping:
  // wins: hunger decreases a bit, growth increases, mood happy
  // losses: health decreases a bit, mood sad
  if (win) {
    st.pet.mood = "happy";
    st.pet.hunger = Math.min(100, st.pet.hunger + 2);     // “fed”
    st.pet.growth = Math.min(100, st.pet.growth + 1.5);
    st.pet.health = Math.min(100, st.pet.health + 0.5);
  } else if (pnl < 0) {
    st.pet.mood = "sad";
    st.pet.health = Math.max(0, st.pet.health - 1.2);
    st.pet.hunger = Math.max(0, st.pet.hunger - 0.6);
    // if repeated losses, show “cryo” / “hurt” vibe
    if (st.pet.health < 35) st.pet.stage = "cryo";
  } else {
    st.pet.mood = "idle";
  }

  st.pet.updated_utc = nowUtc();
  saveState(st);

  logLine("info", "trade", { pnl, market: b.market, side: b.side, paper: b.paper ?? true });
  res.json({ ok: true });
});

// Optional direct pet update (if your Brain v2 wants full control)
app.post("/ingest/pet", (req, res) => {
  const st = loadState();
  const b = req.body && typeof req.body === "object" ? req.body : {};

  if (typeof b.name === "string" && b.name.trim()) st.pet.name = b.name.trim();
  if (typeof b.stage === "string") st.pet.stage = b.stage;
  if (typeof b.mood === "string") st.pet.mood = b.mood;

  if (b.health != null) st.pet.health = Math.max(0, Math.min(100, Number(b.health)));
  if (b.hunger != null) st.pet.hunger = Math.max(0, Math.min(100, Number(b.hunger)));
  if (b.growth != null) st.pet.growth = Math.max(0, Math.min(100, Number(b.growth)));

  st.pet.updated_utc = nowUtc();
  saveState(st);
  logLine("info", "pet_update", b);

  res.json({ ok: true, pet: st.pet });
});

// Bankroll control (dashboard can call this OR /settings)
app.post("/control/bankroll", (req, res) => {
  const settings = loadSettings();
  const v = Number(req.body?.bankroll_gbp);
  if (!Number.isFinite(v) || v <= 0) return res.status(400).json({ ok: false, error: "bad_bankroll" });

  const merged = { ...settings, bankroll_gbp: v };
  saveSettings(merged);
  logLine("info", "bankroll_set", { bankroll_gbp: v });

  res.json({ ok: true, settings: merged });
});

// ---------------- Vault ----------------
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

  logLine("info", "pin_set", {});
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

  res.json({ ok: true, token: st.vault.session_token, ttl_sec: VAULT_TTL_SECONDS, expires_utc: st.vault.session_expires_utc });
});

app.post("/vault/lock", (_req, res) => {
  const st = loadState();
  st.vault.locked = true;
  st.vault.session_token = null;
  st.vault.session_expires_utc = null;
  saveState(st);

  logLine("info", "vault_locked", {});
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

// Debug key read (KEEP FOR DEBUGGING)
app.get("/vault/keys/:id", (req, res) => {
  const st = loadState();
  const chk = requireVaultToken(req, st);
  if (!chk.ok) return res.status(chk.status).json({ ok: false, error: chk.error });

  const entry = (st.vault_keys || []).find((k) => k.id === String(req.params.id));
  if (!entry) return res.status(404).json({ ok: false, error: "not_found" });

  const decrypted = JSON.parse(aesGcmDecrypt(entry.enc));
  res.json({
    ok: true,
    key: { id: entry.id, name: entry.name, exchange: entry.exchange, api_key: decrypted.api_key, api_secret: decrypted.api_secret, created_utc: entry.created_utc },
  });
});

// ---------------- Coinbase (requires vault token + stored key) ----------------
app.get("/coinbase/ping", async (req, res) => {
  const st = loadState();
  const chk = requireVaultToken(req, st);
  if (!chk.ok) return res.status(chk.status).json({ ok: false, error: chk.error });

  const entry = findNewestKeyByName(st, "coinbase_main");
  if (!entry) return res.status(404).json({ ok: false, error: "coinbase_key_not_found" });

  const { api_key, api_secret } = JSON.parse(aesGcmDecrypt(entry.enc));
  const token = buildCoinbaseRestJwt(api_key, api_secret, "GET", COINBASE_ACCOUNTS_PATH);

  try {
    const r = await fetch(COINBASE_BASE + COINBASE_ACCOUNTS_PATH, { method: "GET", headers: { Authorization: `Bearer ${token}` } });

    if (!r.ok) {
      const txt = await r.text();
      return res.status(502).json({ ok: false, error: "coinbase_auth_failed", status: r.status, detail: txt.slice(0, 300) });
    }

    res.json({ ok: true, coinbase: "authenticated", using_key_id: entry.id });
  } catch (e) {
    res.status(502).json({ ok: false, error: "coinbase_fetch_failed", detail: String(e) });
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
    const r = await fetch(COINBASE_BASE + COINBASE_ACCOUNTS_PATH, { method: "GET", headers: { Authorization: `Bearer ${token}` } });

    const data = await r.json().catch(async () => ({ raw: await r.text() }));
    if (!r.ok) return res.status(502).json({ ok: false, error: "coinbase_accounts_failed", status: r.status, detail: data });

    res.json({ ok: true, using_key_id: entry.id, data });
  } catch (e) {
    res.status(502).json({ ok: false, error: "coinbase_fetch_failed", detail: String(e) });
  }
});

// ---------------- Start ----------------
app.listen(PORT, "0.0.0.0", () => {
  ensureDir();
  logLine("info", "server_started", { port: PORT });
  console.log(`crypto-ai-api listening on :${PORT}`);
});

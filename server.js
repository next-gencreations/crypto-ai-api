import express from "express";
import cors from "cors";
import crypto from "crypto";
import fs from "fs";
import path from "path";
import jwt from "jsonwebtoken";

const app = express();

/* ---------------- ENV ---------------- */
const PORT = Number(process.env.PORT || 10000);

// Render persistent disk mount (recommended)
const DATA_DIR = process.env.DATA_DIR || "/var/data";
const SETTINGS_FILE = process.env.SETTINGS_PATH || path.join(DATA_DIR, "settings.json");
const STATE_FILE = process.env.STATE_PATH || path.join(DATA_DIR, "state.json");

// CORS
const CORS_ORIGIN = process.env.CORS_ORIGIN || "*";

// Vault master key (encrypt at rest)
const VAULT_MASTER_KEY = process.env.VAULT_MASTER_KEY || process.env.VAULT_KEY || "";
const VAULT_TTL_SECONDS = Number(process.env.VAULT_TTL_SECONDS || "1800");

// Coinbase
const COINBASE_HOST = "api.coinbase.com";
const COINBASE_BASE = `https://${COINBASE_HOST}`;
const COINBASE_ACCOUNTS_PATH = "/api/v3/brokerage/accounts";

// Binance public candles (for dashboard chart)
const BINANCE_BASE = "https://api.binance.com";
const BINANCE_KLINES_PATH = "/api/v3/klines";

/* ---------------- Helpers ---------------- */
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

    // Dashboard + bot tuning defaults
    markets: ["BTCUSDT", "ETHUSDT"],
    timeframe: "5m",
    bankroll_gbp: 1000,

    companion_name: "Vault Girl",
    companion_sex: "girl",
    vault_number: "13",

    // “Game” knobs (safe defaults)
    paper_enabled: true,
    risk_mode: "safe", // safe | balanced | spicy (still paper by default)
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

    // stored exchange keys (encrypted)
    vault_keys: [],

    // system state for dashboard
    system: {
      open_positions: 0,
      last_heartbeat_utc: null,
      equity: { usd: 0 },
      last_pnl_usd: 0,
    },

    // companion / pet state that UI renders
    pet: {
      name: "Vault Girl",
      stage: "cryo", // cryo | active | ...
      mood: "cryo", // cryo | idle | weak | sick | thriving | ...
      health: 100,
      hunger: 100,
      growth: 0,
      updated_utc: null,
    },

    // rolling logs for /logs
    logs: [],
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
  st.logs.push({
    t: nowUtc(),
    level,
    msg,
    extra,
  });
  // keep last 300
  if (st.logs.length > 300) st.logs = st.logs.slice(-300);
  saveState(st);
}

function sha256Hex(s) {
  return crypto.createHash("sha256").update(String(s)).digest("hex");
}
function hashPin(pin) {
  return sha256Hex(pin);
}

/* -------- AES-256-GCM encryption -------- */
function requireVaultMasterKey() {
  if (!VAULT_MASTER_KEY || VAULT_MASTER_KEY.length < 16) {
    throw new Error("VAULT_MASTER_KEY missing/too short. Set VAULT_MASTER_KEY on Render.");
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

/* -------- Vault session helpers -------- */
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

function ttlSec(st) {
  if (!isVaultUnlocked(st)) return 0;
  return Math.max(
    0,
    Math.floor((new Date(st.vault.session_expires_utc).getTime() - Date.now()) / 1000)
  );
}

/* -------- Coinbase JWT builder --------
payload: {sub:keyName, iss:"cdp", nbf, exp, uri:"METHOD host/path"}
header:  {kid:keyName, nonce:random}
*/
function buildCoinbaseRestJwt(keyName, privateKeyPem, method, requestPath) {
  const now = Math.floor(Date.now() / 1000);
  const uri = `${method.toUpperCase()} ${COINBASE_HOST}${requestPath}`;

  const payload = {
    sub: keyName,
    iss: "cdp",
    nbf: now,
    exp: now + 120,
    uri,
  };

  const header = {
    kid: keyName,
    nonce: crypto.randomBytes(16).toString("hex"),
    typ: "JWT",
  };

  // accept either real PEM or PEM with \n sequences
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

function pickLatestKeyByName(st, name) {
  const keys = (st.vault_keys || []).filter((k) => k.name === name);
  if (!keys.length) return null;
  keys.sort((a, b) => String(b.created_utc).localeCompare(String(a.created_utc)));
  return keys[0];
}

/* ---------------- Middleware ---------------- */
app.use(express.json({ limit: "3mb" }));
app.use(
  cors({
    origin: CORS_ORIGIN === "*" ? true : CORS_ORIGIN,
    credentials: true,
  })
);

/* ---------------- Basic ---------------- */
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

/* ---------------- Settings ---------------- */
app.get("/settings", (_req, res) => {
  res.json({ ok: true, settings: loadSettings() });
});

app.post("/settings", (req, res) => {
  const current = loadSettings();
  const patch = req.body && typeof req.body === "object" ? req.body : {};
  const merged = { ...current, ...patch };
  saveSettings(merged);

  // keep pet name in sync if changed
  const st = loadState();
  if (typeof merged.companion_name === "string" && merged.companion_name.trim()) {
    st.pet.name = merged.companion_name.trim();
    saveState(st);
  }

  logLine("info", "settings_updated", { keys: Object.keys(patch || {}) });
  res.json({ ok: true, settings: merged });
});

/* ---------------- Vault ---------------- */
app.get("/vault/status", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();
  const enabled = settings.vault_enabled !== false;
  const unlocked = enabled ? isVaultUnlocked(st) : false;

  res.json({
    ok: true,
    enabled,
    pin_set: !!st.vault.pin_set,
    unlocked,
    ttl_sec: unlocked ? ttlSec(st) : 0,
  });
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

// Store a key (encrypted)
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

  const entry = {
    id,
    name,
    exchange,
    enc,
    created_utc: nowUtc(),
  };

  st.vault_keys = Array.isArray(st.vault_keys) ? st.vault_keys : [];
  st.vault_keys.push(entry);
  saveState(st);

  logLine("info", "vault_key_added", { exchange, name, id });

  res.json({
    ok: true,
    id,
    name,
    created_utc: entry.created_utc,
    ttl_sec: ttlSec(st),
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
    ttl_sec: ttlSec(st),
    encryption: "aes-256-gcm",
  });
});

// Debug: read one key with secrets (KEEP FOR DEBUGGING)
app.get("/vault/keys/:id", (req, res) => {
  const st = loadState();
  const chk = requireVaultToken(req, st);
  if (!chk.ok) return res.status(chk.status).json({ ok: false, error: chk.error });

  const entry = findVaultKey(st, { id: String(req.params.id) });
  if (!entry) return res.status(404).json({ ok: false, error: "not_found" });

  const decrypted = JSON.parse(aesGcmDecrypt(entry.enc));
  res.json({
    ok: true,
    key: {
      id: entry.id,
      name: entry.name,
      exchange: entry.exchange,
      api_key: decrypted.api_key,
      api_secret: decrypted.api_secret,
      created_utc: entry.created_utc,
    },
  });
});

/* ---------------- Coinbase ---------------- */
app.get("/coinbase/ping", async (req, res) => {
  const st = loadState();
  const chk = requireVaultToken(req, st);
  if (!chk.ok) return res.status(chk.status).json({ ok: false, error: chk.error });

  const entry = pickLatestKeyByName(st, "coinbase_main");
  if (!entry) return res.status(404).json({ ok: false, error: "coinbase_key_not_found" });

  const { api_key, api_secret } = JSON.parse(aesGcmDecrypt(entry.enc));
  const token = buildCoinbaseRestJwt(api_key, api_secret, "GET", COINBASE_ACCOUNTS_PATH);

  // optional debug
  const debug = String(req.query?.debug || "") === "1";
  let jwt_header = null;
  let jwt_payload = null;
  if (debug) {
    const [h, p] = token.split(".").slice(0, 2);
    try {
      jwt_header = JSON.parse(Buffer.from(h, "base64").toString("utf8"));
      jwt_payload = JSON.parse(Buffer.from(p, "base64").toString("utf8"));
    } catch {}
  }

  try {
    const r = await fetch(COINBASE_BASE + COINBASE_ACCOUNTS_PATH, {
      method: "GET",
      headers: { Authorization: `Bearer ${token}` },
    });

    if (!r.ok) {
      const txt = await r.text();
      return res.status(502).json({
        ok: false,
        coinbase_status: r.status,
        using_key_id: entry.id,
        using_key_created_utc: entry.created_utc,
        uri: `GET ${COINBASE_HOST}${COINBASE_ACCOUNTS_PATH}`,
        jwt_header,
        jwt_payload,
        detail: txt.slice(0, 400),
        server_time_utc: nowUtc(),
      });
    }

    return res.json({
      ok: true,
      coinbase: "authenticated",
      coinbase_status: r.status,
      using_key_id: entry.id,
      using_key_created_utc: entry.created_utc,
      ...(debug ? { uri: `GET ${COINBASE_HOST}${COINBASE_ACCOUNTS_PATH}`, jwt_header, jwt_payload, server_time_utc: nowUtc() } : {}),
    });
  } catch (e) {
    return res.status(502).json({ ok: false, error: "coinbase_fetch_failed", detail: String(e) });
  }
});

app.get("/coinbase/accounts", async (req, res) => {
  const st = loadState();
  const chk = requireVaultToken(req, st);
  if (!chk.ok) return res.status(chk.status).json({ ok: false, error: chk.error });

  const entry = pickLatestKeyByName(st, "coinbase_main");
  if (!entry) return res.status(404).json({ ok: false, error: "coinbase_key_not_found" });

  const { api_key, api_secret } = JSON.parse(aesGcmDecrypt(entry.enc));
  const token = buildCoinbaseRestJwt(api_key, api_secret, "GET", COINBASE_ACCOUNTS_PATH);

  try {
    const r = await fetch(COINBASE_BASE + COINBASE_ACCOUNTS_PATH, {
      method: "GET",
      headers: { Authorization: `Bearer ${token}` },
    });

    const data = await r.json().catch(async () => ({ raw: await r.text() }));
    if (!r.ok) return res.status(502).json({ ok: false, error: "coinbase_accounts_failed", status: r.status, detail: data });

    return res.json({ ok: true, using_key_id: entry.id, data });
  } catch (e) {
    return res.status(502).json({ ok: false, error: "coinbase_fetch_failed", detail: String(e) });
  }
});

/* ---------------- Dashboard endpoints ---------------- */
// /status: small summary (dashboard “STATUS” tab)
app.get("/status", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();

  res.json({
    ok: true,
    time_utc: nowUtc(),
    markets: settings.markets || [],
    open_positions: st.system?.open_positions || 0,
    survival: st.pet?.stage || "cryo",
    last_heartbeat_utc: st.system?.last_heartbeat_utc || null,
    equity: st.system?.equity || { usd: 0 },
  });
});

// /logs: rolling log lines
app.get("/logs", (_req, res) => {
  const st = loadState();
  res.json({ ok: true, lines: st.logs || [] });
});

// /data: what your dashboard renders (DATA tab + VAULT tab)
app.get("/data", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();

  res.json({
    ok: true,
    time_utc: nowUtc(),
    markets: settings.markets || [],
    open_positions: st.system?.open_positions || 0,
    survival: st.pet?.stage || "cryo",
    last_heartbeat_utc: st.system?.last_heartbeat_utc || null,
    equity: st.system?.equity || { usd: 0 },
    settings: {
      bankroll_gbp: settings.bankroll_gbp,
      companion_name: settings.companion_name,
      companion_sex: settings.companion_sex,
      vault_number: settings.vault_number,
      risk_mode: settings.risk_mode,
      paper_enabled: settings.paper_enabled,
    },
    pet: st.pet,
    last_pnl_usd: st.system?.last_pnl_usd || 0,
  });
});

// /ohlc: Binance candles (market=BTCUSDT tf=1m|5m|15m|1h limit=500)
app.get("/ohlc", async (req, res) => {
  const market = String(req.query?.market || "BTCUSDT").toUpperCase();
  const tf = String(req.query?.tf || req.query?.timeframe || "5m");
  const limit = Math.min(1000, Math.max(10, Number(req.query?.limit || 500)));

  try {
    const url = new URL(BINANCE_BASE + BINANCE_KLINES_PATH);
    url.searchParams.set("symbol", market);
    url.searchParams.set("interval", tf);
    url.searchParams.set("limit", String(limit));

    const r = await fetch(url.toString());
    const rows = await r.json();

    if (!r.ok) return res.status(502).json({ ok: false, error: "binance_failed", status: r.status, detail: rows });

    // Binance format: [openTime, open, high, low, close, volume, closeTime, ...]
    const candles = Array.isArray(rows)
      ? rows.map((k) => ({
          t: k[0],
          o: Number(k[1]),
          h: Number(k[2]),
          l: Number(k[3]),
          c: Number(k[4]),
          v: Number(k[5]),
        }))
      : [];

    res.json({ ok: true, market, tf, candles });
  } catch (e) {
    res.status(502).json({ ok: false, error: "ohlc_fetch_failed", detail: String(e) });
  }
});

/* ---------------- Bot ↔ Companion loop ---------------- */
/**
 * Bot heartbeat: update status shown in dashboard.
 * POST /bot/heartbeat
 * body: { open_positions, equity_usd, last_pnl_usd }
 */
app.post("/bot/heartbeat", (req, res) => {
  const st = loadState();
  const open_positions = Number(req.body?.open_positions || 0);
  const equity_usd = Number(req.body?.equity_usd || 0);
  const last_pnl_usd = Number(req.body?.last_pnl_usd || 0);

  st.system = st.system || {};
  st.system.open_positions = Number.isFinite(open_positions) ? open_positions : 0;
  st.system.equity = { usd: Number.isFinite(equity_usd) ? equity_usd : 0 };
  st.system.last_pnl_usd = Number.isFinite(last_pnl_usd) ? last_pnl_usd : 0;
  st.system.last_heartbeat_utc = nowUtc();

  // nudge companion mood a bit based on pnl (paper)
  if (last_pnl_usd > 0) {
    st.pet.mood = "thriving";
    st.pet.stage = "active";
  } else if (last_pnl_usd < 0) {
    st.pet.mood = st.pet.health < 60 ? "sick" : "weak";
    st.pet.stage = "active";
  } else {
    if (st.pet.stage !== "cryo") st.pet.mood = "idle";
  }

  st.pet.updated_utc = nowUtc();
  saveState(st);

  logLine("info", "bot_heartbeat", { open_positions, equity_usd, last_pnl_usd });

  res.json({ ok: true });
});

/**
 * Companion update from bot (simple “needs” system).
 * POST /companion/update
 * body: { health, hunger, growth, mood, stage }
 */
app.post("/companion/update", (req, res) => {
  const st = loadState();

  const patch = req.body && typeof req.body === "object" ? req.body : {};
  const h = Number(patch.health);
  const hu = Number(patch.hunger);
  const g = Number(patch.growth);

  if (Number.isFinite(h)) st.pet.health = Math.max(0, Math.min(100, h));
  if (Number.isFinite(hu)) st.pet.hunger = Math.max(0, Math.min(100, hu));
  if (Number.isFinite(g)) st.pet.growth = Math.max(0, g);

  if (typeof patch.mood === "string" && patch.mood.trim()) st.pet.mood = patch.mood.trim();
  if (typeof patch.stage === "string" && patch.stage.trim()) st.pet.stage = patch.stage.trim();

  st.pet.updated_utc = nowUtc();
  saveState(st);

  logLine("info", "companion_update", { health: st.pet.health, hunger: st.pet.hunger, mood: st.pet.mood, stage: st.pet.stage });
  res.json({ ok: true, pet: st.pet });
});

/**
 * Paper trading event feed:
 * POST /paper/event
 * body: { pnl_usd, reason }
 */
app.post("/paper/event", (req, res) => {
  const st = loadState();

  const pnl = Number(req.body?.pnl_usd || 0);
  const reason = String(req.body?.reason || "trade");

  // hunger/health drift
  // - losses drain health/hunger a bit
  // - wins restore a bit
  const delta = pnl > 0 ? 2 : pnl < 0 ? -3 : 0;

  st.pet.hunger = Math.max(0, Math.min(100, st.pet.hunger + delta));
  st.pet.health = Math.max(0, Math.min(100, st.pet.health + (delta > 0 ? 1 : delta < 0 ? -2 : 0)));

  if (st.pet.health <= 20 || st.pet.hunger <= 15) {
    st.pet.mood = "sick";
    st.pet.stage = "cryo";
  } else if (pnl > 0) {
    st.pet.mood = "happy";
    st.pet.stage = "active";
  } else if (pnl < 0) {
    st.pet.mood = "weak";
    st.pet.stage = "active";
  } else {
    st.pet.mood = "idle";
    st.pet.stage = "active";
  }

  st.pet.updated_utc = nowUtc();
  saveState(st);

  logLine("info", "paper_event", { pnl_usd: pnl, reason, pet: { health: st.pet.health, hunger: st.pet.hunger, mood: st.pet.mood, stage: st.pet.stage } });

  res.json({ ok: true, pet: st.pet });
});

/* ---------------- Start ---------------- */
app.listen(PORT, "0.0.0.0", () => {
  ensureDir();
  logLine("info", "server_started", { port: PORT });
  console.log(`crypto-ai-api listening on :${PORT}`);
});

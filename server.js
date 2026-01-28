import express from "express";
import cors from "cors";
import crypto from "crypto";
import fs from "fs";
import path from "path";
import jwt from "jsonwebtoken";

const app = express();

// ---------------- ENV ----------------
const PORT = Number(process.env.PORT || 10000);

// Persisted storage directory on Render
const DATA_DIR = process.env.DATA_DIR || "/var/data";
const SETTINGS_FILE = process.env.SETTINGS_PATH || path.join(DATA_DIR, "settings.json");
const STATE_FILE = process.env.STATE_PATH || path.join(DATA_DIR, "state.json");
const LOG_FILE = process.env.LOG_PATH || path.join(DATA_DIR, "logs.jsonl");

// CORS
const CORS_ORIGIN = process.env.CORS_ORIGIN || "*";

// Coinbase (Advanced Trade REST)
const COINBASE_HOST = "api.coinbase.com";
const COINBASE_BASE = `https://${COINBASE_HOST}`;
const COINBASE_ACCOUNTS_PATH = "/api/v3/brokerage/accounts";

// Vault master key (encrypt keys at rest)
const VAULT_MASTER_KEY = process.env.VAULT_MASTER_KEY || process.env.VAULT_KEY || "";
const VAULT_TTL_SECONDS = Number(process.env.VAULT_TTL_SECONDS || "1800");

// ---------------- Helpers ----------------
function ensureDir() {
  fs.mkdirSync(DATA_DIR, { recursive: true });
}

function nowUtc() {
  return new Date().toISOString();
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

function appendLog(lineObj) {
  try {
    ensureDir();
    fs.appendFileSync(LOG_FILE, JSON.stringify({ t: nowUtc(), ...lineObj }) + "\n");
  } catch {
    // ignore logging errors
  }
}

function defaultSettings() {
  return {
    vault_enabled: true,

    // Dashboard settings
    markets: ["BTCUSDT", "ETHUSDT"],
    bankroll_gbp: 100,

    // “Companion” config
    companion_name: "Vault Girl",
  };
}

function defaultState() {
  return {
    vault: {
      enabled: true,
      pin_set: false,
      pin_hash: null,
      session_token: null,
      session_expires_utc: null,
    },
    vault_keys: [], // [{id,name,exchange,enc,created_utc}]

    // Paper trading state (fake trading)
    paper: {
      equity_usd: 0,
      open_positions: 0,
      wins: 0,
      losses: 0,
      last_heartbeat_utc: null,
    },

    // Companion state (Vault Girl / Boy reactions)
    companion: {
      stage: "cryo",
      mood: "idle",
      health: 100,
      hunger: 100,
      growth: 0,
      updated_utc: null,
      last_reaction: null,
    },
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

function sha256Hex(s) {
  return crypto.createHash("sha256").update(String(s)).digest("hex");
}

function requireVaultMasterKey() {
  if (!VAULT_MASTER_KEY || VAULT_MASTER_KEY.length < 16) {
    throw new Error("VAULT_MASTER_KEY missing/too short. Set it in Render env vars.");
  }
}

function keyBytes() {
  return crypto.createHash("sha256").update(VAULT_MASTER_KEY).digest(); // 32 bytes
}

// AES-256-GCM encrypt/decrypt for key material
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

// Find stored key by name or id
function findVaultKey(st, { name, id }) {
  if (!Array.isArray(st?.vault_keys)) return null;
  if (name) return st.vault_keys.find((k) => k.name === name) || null;
  if (id) return st.vault_keys.find((k) => k.id === id) || null;
  return null;
}

// Coinbase JWT generation (ES256)
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

  // If stored key has "\n" sequences, convert to real newlines.
  const pem = String(privateKeyPem).includes("\\n")
    ? String(privateKeyPem).replace(/\\n/g, "\n")
    : String(privateKeyPem);

  return jwt.sign(payload, pem, { algorithm: "ES256", header });
}

// Companion reaction to paper P&L
function applyCompanionReaction(st, pnl) {
  const c = st.companion || defaultState().companion;
  const p = st.paper || defaultState().paper;

  // basic “feelings”
  if (pnl > 0) {
    c.mood = "happy";
    c.hunger = Math.min(100, c.hunger + 3);
    c.health = Math.min(100, c.health + 2);
    c.growth = Math.min(100, c.growth + 1);
    c.last_reaction = `Win +$${pnl.toFixed(2)} (feeds companion)`;
  } else if (pnl < 0) {
    c.mood = "sad";
    c.hunger = Math.max(0, c.hunger - 4);
    c.health = Math.max(0, c.health - 2);
    c.growth = Math.max(0, c.growth - 1);
    c.last_reaction = `Loss -$${Math.abs(pnl).toFixed(2)} (companion feels it)`;
  } else {
    c.mood = "idle";
    c.last_reaction = "No change";
  }

  // stage logic
  if (c.health <= 25 || c.hunger <= 25) c.stage = "cryo";
  else if (c.growth >= 50) c.stage = "active";
  else c.stage = "warming";

  c.updated_utc = nowUtc();
  st.companion = c;
  st.paper = p;
}

// ---------------- Middleware ----------------
app.use(express.json({ limit: "2mb" }));
app.use(cors({ origin: CORS_ORIGIN === "*" ? true : CORS_ORIGIN, credentials: true }));

// ---------------- Basic ----------------
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

// ---------------- Settings (used by dashboard “SAVE”) ----------------
app.get("/settings", (_req, res) => res.json(loadSettings()));

app.post("/settings", (req, res) => {
  const current = loadSettings();
  const patch = req.body && typeof req.body === "object" ? req.body : {};
  const merged = { ...current, ...patch };
  saveSettings(merged);
  appendLog({ level: "info", msg: "settings_updated", patch: Object.keys(patch) });
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
  st.vault.pin_hash = sha256Hex(pin);
  st.vault.pin_set = true;
  st.vault.session_token = null;
  st.vault.session_expires_utc = null;
  saveState(st);

  appendLog({ level: "info", msg: "pin_set" });
  res.json({ ok: true });
});

app.post("/vault/unlock", (req, res) => {
  const settings = loadSettings();
  if (settings.vault_enabled === false) return res.status(403).json({ ok: false, error: "vault_disabled" });

  const pin = String(req.body?.pin || "");
  const st = loadState();

  if (!st.vault.pin_set || !st.vault.pin_hash) return res.status(400).json({ ok: false, error: "pin_not_set" });
  if (sha256Hex(pin) !== st.vault.pin_hash) return res.status(401).json({ ok: false, error: "bad_pin" });

  st.vault.session_token = crypto.randomBytes(16).toString("hex");
  st.vault.session_expires_utc = new Date(Date.now() + VAULT_TTL_SECONDS * 1000).toISOString();
  saveState(st);

  appendLog({ level: "info", msg: "vault_unlocked", ttl_sec: VAULT_TTL_SECONDS });

  res.json({
    ok: true,
    token: st.vault.session_token,
    ttl_sec: VAULT_TTL_SECONDS,
    expires_utc: st.vault.session_expires_utc,
  });
});

app.post("/vault/lock", (_req, res) => {
  const st = loadState();
  st.vault.session_token = null;
  st.vault.session_expires_utc = null;
  saveState(st);

  appendLog({ level: "info", msg: "vault_locked" });
  res.json({ ok: true });
});

// Store key (encrypted)
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
  // replace any existing key with same name (keep it simple)
  st.vault_keys = st.vault_keys.filter((k) => k.name !== name);
  st.vault_keys.push(entry);
  saveState(st);

  appendLog({ level: "info", msg: "key_saved", exchange, name });

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

// Debug (optional): get one key with secrets (keep only while debugging)
app.get("/vault/keys/:id", (req, res) => {
  const st = loadState();
  const chk = requireVaultToken(req, st);
  if (!chk.ok) return res.status(chk.status).json({ ok: false, error: chk.error });

  const entry = findVaultKey(st, { id: String(req.params.id) });
  if (!entry) return res.status(404).json({ ok: false, error: "not_found" });

  const decrypted = JSON.parse(aesGcmDecrypt(entry.enc));
  res.json({
    ok: true,
    key: { id: entry.id, name: entry.name, exchange: entry.exchange, ...decrypted, created_utc: entry.created_utc },
  });
});

// ---------------- Coinbase (read-only) ----------------
async function coinbaseAuthedFetch(st, method, requestPath) {
  const entry = findVaultKey(st, { name: "coinbase_main" });
  if (!entry) return { ok: false, status: 404, error: "coinbase_key_not_found" };

  const { api_key, api_secret } = JSON.parse(aesGcmDecrypt(entry.enc));
  const token = buildCoinbaseRestJwt(api_key, api_secret, method, requestPath);

  const r = await fetch(COINBASE_BASE + requestPath, {
    method,
    headers: { Authorization: `Bearer ${token}` },
  });

  return { ok: r.ok, status: r.status, entry_id: entry.id, entry_created_utc: entry.created_utc, response: r };
}

app.get("/coinbase/ping", async (req, res) => {
  const st = loadState();
  const chk = requireVaultToken(req, st);
  if (!chk.ok) return res.status(chk.status).json({ ok: false, error: chk.error });

  try {
    const out = await coinbaseAuthedFetch(st, "GET", COINBASE_ACCOUNTS_PATH);
    if (!out.ok) {
      const txt = await out.response.text().catch(() => "");
      appendLog({ level: "warn", msg: "coinbase_ping_failed", status: out.status });
      return res.status(502).json({ ok: false, error: "coinbase_auth_failed", status: out.status, detail: txt.slice(0, 300) });
    }
    appendLog({ level: "info", msg: "coinbase_ping_ok" });
    return res.json({ ok: true, coinbase: "authenticated", using_key_id: out.entry_id });
  } catch (e) {
    appendLog({ level: "error", msg: "coinbase_ping_exception", detail: String(e) });
    return res.status(502).json({ ok: false, error: "coinbase_fetch_failed", detail: String(e) });
  }
});

app.get("/coinbase/accounts", async (req, res) => {
  const st = loadState();
  const chk = requireVaultToken(req, st);
  if (!chk.ok) return res.status(chk.status).json({ ok: false, error: chk.error });

  try {
    const out = await coinbaseAuthedFetch(st, "GET", COINBASE_ACCOUNTS_PATH);
    const data = await out.response.json().catch(async () => ({ raw: await out.response.text() }));

    if (!out.ok) {
      appendLog({ level: "warn", msg: "coinbase_accounts_failed", status: out.status });
      return res.status(502).json({ ok: false, error: "coinbase_accounts_failed", status: out.status, detail: data });
    }

    appendLog({ level: "info", msg: "coinbase_accounts_ok" });
    return res.json({ ok: true, using_key_id: out.entry_id, data });
  } catch (e) {
    appendLog({ level: "error", msg: "coinbase_accounts_exception", detail: String(e) });
    return res.status(502).json({ ok: false, error: "coinbase_fetch_failed", detail: String(e) });
  }
});

// ---------------- Logs (dashboard expects { lines: [] }) ----------------
app.get("/logs", (_req, res) => {
  try {
    if (!fs.existsSync(LOG_FILE)) return res.json({ ok: true, lines: [] });
    const lines = fs
      .readFileSync(LOG_FILE, "utf-8")
      .split("\n")
      .filter(Boolean)
      .slice(-200)
      .map((l) => {
        try { return JSON.parse(l); } catch { return { t: nowUtc(), raw: l }; }
      });
    return res.json({ ok: true, lines });
  } catch (e) {
    return res.json({ ok: true, lines: [{ t: nowUtc(), level: "error", msg: "logs_read_failed", detail: String(e) }] });
  }
});

// ---------------- DATA (this fixes your dashboard “Cannot GET /data”) ----------------
app.get("/data", async (_req, res) => {
  const settings = loadSettings();
  const st = loadState();

  // heartbeat
  st.paper.last_heartbeat_utc = nowUtc();
  saveState(st);

  const vaultUnlocked = isVaultUnlocked(st);

  // Try to attach a lightweight Coinbase snapshot if unlocked
  let coinbase = { ok: false, note: "vault_locked" };
  if (vaultUnlocked) {
    try {
      const out = await coinbaseAuthedFetch(st, "GET", COINBASE_ACCOUNTS_PATH);
      if (out.ok) {
        const data = await out.response.json().catch(() => null);
        coinbase = { ok: true, using_key_id: out.entry_id, accounts: data?.accounts || null };
      } else {
        const txt = await out.response.text().catch(() => "");
        coinbase = { ok: false, status: out.status, detail: txt.slice(0, 120) };
      }
    } catch (e) {
      coinbase = { ok: false, error: String(e) };
    }
  }

  // Dashboard-friendly shape
  const payload = {
    ok: true,
    time_utc: nowUtc(),

    markets: settings.markets || ["BTCUSDT"],
    open_positions: st.paper.open_positions || 0,

    survival: st.companion?.stage || "cryo",
    last_heartbeat_utc: st.paper.last_heartbeat_utc,

    equity: { usd: st.paper.equity_usd || 0 },

    settings: {
      bankroll_gbp: settings.bankroll_gbp ?? 100,
      companion_name: settings.companion_name ?? "Vault Girl",
    },

    pet: {
      name: settings.companion_name ?? "Vault Girl",
      stage: st.companion?.stage ?? "cryo",
      mood: st.companion?.mood ?? "idle",
      health: st.companion?.health ?? 100,
      hunger: st.companion?.hunger ?? 100,
      growth: st.companion?.growth ?? 0,
      updated_utc: st.companion?.updated_utc ?? null,
      last_reaction: st.companion?.last_reaction ?? null,
    },

    stats: {
      wins: st.paper.wins || 0,
      losses: st.paper.losses || 0,
    },

    vault: {
      enabled: settings.vault_enabled !== false,
      unlocked: vaultUnlocked,
    },

    coinbase,
  };

  return res.json(payload);
});

// ---------------- Paper trading (simple “feel wins/losses”) ----------------
app.post("/paper/reset", (req, res) => {
  const st = loadState();
  st.paper.equity_usd = Number(req.body?.equity_usd ?? 0);
  st.paper.open_positions = 0;
  st.paper.wins = 0;
  st.paper.losses = 0;

  st.companion = defaultState().companion;
  st.companion.updated_utc = nowUtc();

  saveState(st);
  appendLog({ level: "info", msg: "paper_reset", equity_usd: st.paper.equity_usd });
  res.json({ ok: true });
});

// Step with a fake pnl value (e.g. +5 or -3)
app.post("/paper/step", (req, res) => {
  const st = loadState();
  const pnl = Number(req.body?.pnl ?? 0);

  st.paper.equity_usd = Number(st.paper.equity_usd || 0) + pnl;
  if (pnl > 0) st.paper.wins = (st.paper.wins || 0) + 1;
  if (pnl < 0) st.paper.losses = (st.paper.losses || 0) + 1;

  applyCompanionReaction(st, pnl);

  saveState(st);
  appendLog({ level: "info", msg: "paper_step", pnl, equity_usd: st.paper.equity_usd, mood: st.companion.mood });

  res.json({ ok: true, pnl, equity_usd: st.paper.equity_usd, pet: st.companion });
});

// ---------------- Start ----------------
app.listen(PORT, "0.0.0.0", () => {
  ensureDir();
  appendLog({ level: "info", msg: "server_started", port: PORT });
  console.log(`crypto-ai-api listening on :${PORT}`);
});

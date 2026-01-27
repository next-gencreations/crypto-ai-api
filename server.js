import express from "express";
import cors from "cors";
import crypto from "crypto";
import fs from "fs";
import path from "path";
import jwt from "jsonwebtoken";

const app = express();

/* ===================== ENV ===================== */
const PORT = Number(process.env.PORT || 10000);

// Render persistent disk (recommended)
const DATA_DIR = process.env.DATA_DIR || "/var/data";
const SETTINGS_FILE = process.env.SETTINGS_PATH || path.join(DATA_DIR, "settings.json");
const STATE_FILE = process.env.STATE_PATH || path.join(DATA_DIR, "state.json");

// CORS
const CORS_ORIGIN = process.env.CORS_ORIGIN || "*";

// Vault encryption master key (MUST set on Render)
const VAULT_MASTER_KEY = process.env.VAULT_MASTER_KEY || process.env.VAULT_KEY || "";
const VAULT_TTL_SECONDS = Number(process.env.VAULT_TTL_SECONDS || "1800");

// Coinbase
const COINBASE_HOST = "api.coinbase.com";
const COINBASE_BASE = `https://${COINBASE_HOST}`;
const COINBASE_ACCOUNTS_PATH = "/api/v3/brokerage/accounts";

/* ===================== File helpers ===================== */
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

/* ===================== Settings/State ===================== */
function defaultSettings() {
  return {
    vault_enabled: true,
    build_tag: process.env.NEXT_PUBLIC_BUILD_TAG || "v1",
  };
}

function defaultState() {
  return {
    vault: {
      pin_hash: null,
      pin_set: false,
      locked: true,
      session_token: null,
      session_expires_utc: null,
    },
    // [{id,name,exchange,enc,created_utc}]
    vault_keys: [],
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

/* ===================== Hashing ===================== */
function sha256Hex(s) {
  return crypto.createHash("sha256").update(String(s)).digest("hex");
}
function hashPin(pin) {
  return sha256Hex(pin);
}

/* ===================== AES-256-GCM ===================== */
function requireVaultMasterKey() {
  if (!VAULT_MASTER_KEY || VAULT_MASTER_KEY.length < 16) {
    throw new Error("VAULT_MASTER_KEY is missing/too short. Set VAULT_MASTER_KEY in Render env vars.");
  }
}
function keyBytes() {
  // Derive 32 bytes from master key string
  return crypto.createHash("sha256").update(VAULT_MASTER_KEY).digest();
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

/* ===================== Vault auth ===================== */
function isVaultUnlocked(st) {
  if (!st?.vault?.session_token) return false;
  if (!st?.vault?.session_expires_utc) return false;
  return new Date(st.vault.session_expires_utc).getTime() > Date.now();
}

function vaultTtlSec(st) {
  if (!isVaultUnlocked(st)) return 0;
  const ms = new Date(st.vault.session_expires_utc).getTime() - Date.now();
  return Math.max(0, Math.floor(ms / 1000));
}

function requireVaultToken(req, st) {
  const token = req.headers["x-vault-token"];
  if (!token) return { ok: false, status: 401, error: "missing_vault_token" };
  if (!isVaultUnlocked(st)) return { ok: false, status: 401, error: "vault_locked" };
  if (String(token) !== String(st.vault.session_token)) return { ok: false, status: 401, error: "bad_vault_token" };
  return { ok: true };
}

/* ===================== Key selection (FIX) ===================== */
/**
 * IMPORTANT FIX:
 * If multiple keys share the same name, always use the newest one (by created_utc).
 * This solves your "coinbase_main duplicated" issue.
 */
function findVaultKey(st, { name, id }) {
  if (!st?.vault_keys?.length) return null;

  if (id) return st.vault_keys.find((k) => k.id === id) || null;

  if (name) {
    const matches = st.vault_keys.filter((k) => k.name === name);
    if (!matches.length) return null;

    matches.sort((a, b) => {
      const ta = Date.parse(a.created_utc || 0) || 0;
      const tb = Date.parse(b.created_utc || 0) || 0;
      return tb - ta;
    });

    return matches[0];
  }

  return null;
}

/* ===================== Coinbase JWT ===================== */
/**
 * Coinbase REST JWT:
 * - payload: {sub:key_name, iss:"cdp", nbf, exp, uri:"METHOD host/path"}
 * - header: {kid:key_name, nonce: random}
 */
function buildCoinbaseRestJwt(keyName, privateKeyPem, method, requestPath) {
  const now = Math.floor(Date.now() / 1000);
  const uri = `${method.toUpperCase()} ${COINBASE_HOST}${requestPath}`;

  // Normalize PEM: replace literal "\n" with newlines and ensure it ends with newline
  const pem1 = String(privateKeyPem).includes("\\n")
    ? String(privateKeyPem).replace(/\\n/g, "\n")
    : String(privateKeyPem);
  const pem = pem1.endsWith("\n") ? pem1 : pem1 + "\n";

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
  };

  return jwt.sign(payload, pem, { algorithm: "ES256", header });
}

/* ===================== Middleware ===================== */
app.use(express.json({ limit: "2mb" }));
app.use(
  cors({
    origin: CORS_ORIGIN === "*" ? true : CORS_ORIGIN,
    credentials: true,
  })
);

/* ===================== Base routes ===================== */
app.get("/", (_req, res) => {
  res.json({ ok: true, service: "crypto-ai-api", time_utc: nowUtc() });
});

app.get("/health", (_req, res) => {
  const settings = loadSettings();
  const st = loadState();
  res.json({
    ok: true,
    status: 200,
    service: "crypto-ai-api",
    time_utc: nowUtc(),
    vault_enabled: settings.vault_enabled !== false,
    vault_unlocked: isVaultUnlocked(st),
    ttl_sec: vaultTtlSec(st),
  });
});

/* ===================== Settings ===================== */
app.get("/settings", (_req, res) => res.json(loadSettings()));

app.post("/settings", (req, res) => {
  const current = loadSettings();
  const patch = req.body && typeof req.body === "object" ? req.body : {};
  const merged = { ...current, ...patch };
  saveSettings(merged);
  res.json({ ok: true, settings: merged });
});

/* ===================== Vault ===================== */
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
    ttl_sec: unlocked ? vaultTtlSec(st) : 0,
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
  res.json({ ok: true });
});

/* ===================== Vault Keys ===================== */
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

  st.vault_keys = Array.isArray(st.vault_keys) ? st.vault_keys : [];
  st.vault_keys.push({ id, name, exchange, enc, created_utc: nowUtc() });
  saveState(st);

  res.json({
    ok: true,
    id,
    name,
    created_utc: nowUtc(),
    ttl_sec: vaultTtlSec(st),
    encryption: "aes-256-gcm",
  });
});

app.get("/vault/keys", (req, res) => {
  const st = loadState();
  const chk = requireVaultToken(req, st);
  if (!chk.ok) return res.status(chk.status).json({ ok: false, error: chk.error });

  res.json({
    ok: true,
    keys: (st.vault_keys || []).map((k) => ({ id: k.id, name: k.name, created_utc: k.created_utc })),
    ttl_sec: vaultTtlSec(st),
    encryption: "aes-256-gcm",
  });
});

// KEEP FOR DEBUGGING (returns decrypted secret) — requires vault token
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

// DELETE old keys (so you can remove duplicates)
app.delete("/vault/keys/:id", (req, res) => {
  const st = loadState();
  const chk = requireVaultToken(req, st);
  if (!chk.ok) return res.status(chk.status).json({ ok: false, error: chk.error });

  const id = String(req.params.id);
  const before = (st.vault_keys || []).length;
  st.vault_keys = (st.vault_keys || []).filter((k) => k.id !== id);
  const after = st.vault_keys.length;

  saveState(st);
  res.json({ ok: true, removed: before - after });
});

/* ===================== Coinbase ===================== */
app.get("/coinbase/ping", async (req, res) => {
  const st = loadState();
  const chk = requireVaultToken(req, st);
  if (!chk.ok) return res.status(chk.status).json({ ok: false, error: chk.error });

  // Always uses newest "coinbase_main" because findVaultKey sorts by created_utc
  const entry = findVaultKey(st, { name: "coinbase_main" });
  if (!entry) return res.status(404).json({ ok: false, error: "coinbase_key_not_found" });

  const { api_key, api_secret } = JSON.parse(aesGcmDecrypt(entry.enc));

  const method = "GET";
  const pathOnly = COINBASE_ACCOUNTS_PATH;
  const uri = `${method} ${COINBASE_HOST}${pathOnly}`;

  const token = buildCoinbaseRestJwt(api_key, api_secret, method, pathOnly);

  try {
    const r = await fetch(COINBASE_BASE + pathOnly, {
      method,
      headers: { Authorization: `Bearer ${token}` },
    });

    // Debug mode: returns JWT header/payload (NO signature), uri, server time
    if (req.query.debug === "1") {
      const [h, p] = token.split(".");
      const jwt_header = JSON.parse(Buffer.from(h, "base64url").toString("utf8"));
      const jwt_payload = JSON.parse(Buffer.from(p, "base64url").toString("utf8"));
      return res.status(r.ok ? 200 : 502).json({
        ok: r.ok,
        coinbase_status: r.status,
        using_key_id: entry.id,
        using_key_created_utc: entry.created_utc,
        uri,
        jwt_header,
        jwt_payload,
        server_time_utc: nowUtc(),
      });
    }

    if (!r.ok) {
      const txt = await r.text();
      return res.status(502).json({
        ok: false,
        error: "coinbase_auth_failed",
        status: r.status,
        using_key_id: entry.id,
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

  const entry = findVaultKey(st, { name: "coinbase_main" });
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
        using_key_id: entry.id,
        detail: data,
      });
    }

    return res.json({ ok: true, using_key_id: entry.id, data });
  } catch (e) {
    return res.status(502).json({ ok: false, error: "coinbase_fetch_failed", detail: String(e) });
  }
});

/* ===================== Start ===================== */
app.listen(PORT, "0.0.0.0", () => {
  ensureDir();
  console.log(`crypto-ai-api listening on :${PORT}`);
});

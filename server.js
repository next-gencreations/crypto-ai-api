import express from "express";
import cors from "cors";
import crypto from "crypto";
import fs from "fs";
import path from "path";
import jwt from "jsonwebtoken";

const app = express();

/* ===================== ENV ===================== */
const PORT = Number(process.env.PORT || 10000);

const DATA_DIR = process.env.DATA_DIR || "/var/data";
const SETTINGS_FILE = process.env.SETTINGS_PATH || path.join(DATA_DIR, "settings.json");
const STATE_FILE = process.env.STATE_PATH || path.join(DATA_DIR, "state.json");

const CORS_ORIGIN = process.env.CORS_ORIGIN || "*";

const VAULT_MASTER_KEY = process.env.VAULT_MASTER_KEY || process.env.VAULT_KEY || "";
const VAULT_TTL_SECONDS = Number(process.env.VAULT_TTL_SECONDS || "1800");

const COINBASE_HOST = "api.coinbase.com";
const COINBASE_BASE = `https://${COINBASE_HOST}`;
const COINBASE_ACCOUNTS_PATH = "/api/v3/brokerage/accounts";

/* ===================== Helpers ===================== */
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
  return { vault_enabled: true, build_tag: process.env.NEXT_PUBLIC_BUILD_TAG || "v1" };
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
    vault_keys: [], // [{id,name,exchange,enc,created_utc}]
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

function hashPin(pin) {
  return sha256Hex(pin);
}

/* ===================== AES-256-GCM ===================== */
function requireVaultMasterKey() {
  if (!VAULT_MASTER_KEY || VAULT_MASTER_KEY.length < 16) {
    throw new Error("VAULT_MASTER_KEY is missing/too short. Set VAULT_MASTER_KEY in Render.");
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

/* ===================== Vault helpers ===================== */
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

function findVaultKey(st, { name, id }) {
  if (!st?.vault_keys?.length) return null;
  if (name) return st.vault_keys.find((k) => k.name === name) || null;
  if (id) return st.vault_keys.find((k) => k.id === id) || null;
  return null;
}

/* ===================== Coinbase JWT ===================== */
/**
 * Coinbase requires:
 * uri = "{METHOD} {HOST}{PATH}"
 * headers: kid = key name, nonce = random
 * exp within ~2 minutes
 */
function buildCoinbaseRestJwt(keyName, privateKeyPem, method, requestPath) {
  const now = Math.floor(Date.now() / 1000);
  const uri = `${method.toUpperCase()} ${COINBASE_HOST}${requestPath}`;

  const payload = { sub: keyName, iss: "cdp", nbf: now, exp: now + 120, uri };

  const header = { kid: keyName, nonce: crypto.randomBytes(16).toString("hex") };

  // Normalize PEM: turn \n into newlines and ensure it ends with newline
  const pem1 = String(privateKeyPem).includes("\\n")
    ? String(privateKeyPem).replace(/\\n/g, "\n")
    : String(privateKeyPem);
  const pem = pem1.endsWith("\n") ? pem1 : pem1 + "\n";

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

/* ===================== Routes ===================== */
app.get("/", (_req, res) => res.json({ ok: true, service: "crypto-ai-api", time_utc: nowUtc() }));

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
  });
});

/* ---------- Settings ---------- */
app.get("/settings", (_req, res) => res.json(loadSettings()));

app.post("/settings", (req, res) => {
  const merged = { ...loadSettings(), ...(req.body && typeof req.body === "object" ? req.body : {}) };
  saveSettings(merged);
  res.json({ ok: true, settings: merged });
});

/* ---------- Vault ---------- */
app.get("/vault/status", (_req, res) => {
  const st = loadState();
  const unlocked = isVaultUnlocked(st);
  const ttl = unlocked
    ? Math.max(0, Math.floor((new Date(st.vault.session_expires_utc).getTime() - Date.now()) / 1000))
    : 0;
  res.json({ ok: true, enabled: true, pin_set: !!st.vault.pin_set, unlocked, ttl_sec: ttl });
});

app.post("/vault/set-pin", (req, res) => {
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
  const pin = String(req.body?.pin || "");
  const st = loadState();

  if (!st.vault.pin_set || !st.vault.pin_hash) return res.status(400).json({ ok: false, error: "pin_not_set" });
  if (hashPin(pin) !== st.vault.pin_hash) return res.status(401).json({ ok: false, error: "bad_pin" });

  st.vault.locked = false;
  st.vault.session_token = crypto.randomBytes(16).toString("hex");
  st.vault.session_expires_utc = new Date(Date.now() + VAULT_TTL_SECONDS * 1000).toISOString();
  saveState(st);

  res.json({ ok: true, token: st.vault.session_token, ttl_sec: VAULT_TTL_SECONDS, expires_utc: st.vault.session_expires_utc });
});

app.post("/vault/lock", (_req, res) => {
  const st = loadState();
  st.vault.locked = true;
  st.vault.session_token = null;
  st.vault.session_expires_utc = null;
  saveState(st);
  res.json({ ok: true });
});

/* ---------- Vault Keys ---------- */
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
    ttl_sec: Math.max(0, Math.floor((new Date(st.vault.session_expires_utc).getTime() - Date.now()) / 1000)),
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
    ttl_sec: Math.max(0, Math.floor((new Date(st.vault.session_expires_utc).getTime() - Date.now()) / 1000)),
    encryption: "aes-256-gcm",
  });
});

/* KEEP FOR DEBUGGING: returns secrets only when unlocked + token */
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

/* ---------- Coinbase (1) Ping + Debug ---------- */
app.get("/coinbase/ping", async (req, res) => {
  const st = loadState();
  const chk = requireVaultToken(req, st);
  if (!chk.ok) return res.status(chk.status).json({ ok: false, error: chk.error });

  const entry = findVaultKey(st, { name: "coinbase_main" });
  if (!entry) return res.status(404).json({ ok: false, error: "coinbase_key_not_found" });

  const { api_key, api_secret } = JSON.parse(aesGcmDecrypt(entry.enc));

  const method = "GET";
  const pathOnly = COINBASE_ACCOUNTS_PATH; // no query, no trailing slash
  const uri = `${method} ${COINBASE_HOST}${pathOnly}`;

  const token = buildCoinbaseRestJwt(api_key, api_secret, method, pathOnly);

  try {
    const r = await fetch(COINBASE_BASE + pathOnly, {
      method,
      headers: { Authorization: `Bearer ${token}` },
    });

    // debug=1 returns header/payload (no signature) + uri + server time
    if (req.query.debug === "1") {
      const [h, p] = token.split(".");
      const jwt_header = JSON.parse(Buffer.from(h, "base64url").toString("utf8"));
      const jwt_payload = JSON.parse(Buffer.from(p, "base64url").toString("utf8"));
      return res.status(r.ok ? 200 : 502).json({
        ok: r.ok,
        coinbase_status: r.status,
        uri,
        jwt_header,
        jwt_payload,
        server_time_utc: nowUtc(),
        note:
          "If coinbase_status=401: check Coinbase key permissions and IP allowlist; ensure api_key matches the private key pair; uri must match EXACT endpoint.",
      });
    }

    if (!r.ok) {
      const txt = await r.text();
      return res.status(502).json({ ok: false, error: "coinbase_auth_failed", status: r.status, detail: txt.slice(0, 300) });
    }

    return res.json({ ok: true, coinbase: "authenticated" });
  } catch (e) {
    return res.status(502).json({ ok: false, error: "coinbase_fetch_failed", detail: String(e) });
  }
});

/* ---------- Coinbase (2) Accounts ---------- */
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

    if (!r.ok) return res.status(502).json({ ok: false, error: "coinbase_accounts_failed", status: r.status, detail: data });

    return res.json({ ok: true, data });
  } catch (e) {
    return res.status(502).json({ ok: false, error: "coinbase_fetch_failed", detail: String(e) });
  }
});

/* ---------- (3) Bot runner (read-only template) ---------- */
app.post("/bot/run", async (req, res) => {
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
    if (!r.ok) return res.status(502).json({ ok: false, error: "bot_coinbase_failed", status: r.status, detail: data });

    const accounts = data?.accounts || data?.data?.accounts || [];
    return res.json({ ok: true, bot: { ran_utc: nowUtc(), accounts_found: Array.isArray(accounts) ? accounts.length : 0 } });
  } catch (e) {
    return res.status(502).json({ ok: false, error: "bot_run_failed", detail: String(e) });
  }
});

/* ===================== Start ===================== */
app.listen(PORT, "0.0.0.0", () => {
  ensureDir();
  console.log(`crypto-ai-api listening on :${PORT}`);
});

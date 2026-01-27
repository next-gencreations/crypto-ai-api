import express from "express";
import cors from "cors";
import crypto from "crypto";
import fs from "fs";
import path from "path";
import jwt from "jsonwebtoken";

const app = express();

/* ================= ENV ================= */
const PORT = Number(process.env.PORT || 10000);
const DATA_DIR = process.env.DATA_DIR || "/var/data";
const SETTINGS_FILE = path.join(DATA_DIR, "settings.json");
const STATE_FILE = path.join(DATA_DIR, "state.json");
const CORS_ORIGIN = process.env.CORS_ORIGIN || "*";

const VAULT_MASTER_KEY = process.env.VAULT_MASTER_KEY;
const VAULT_TTL_SECONDS = Number(process.env.VAULT_TTL_SECONDS || "1800");

const COINBASE_HOST = "api.coinbase.com";
const COINBASE_BASE = `https://${COINBASE_HOST}`;
const COINBASE_ACCOUNTS_PATH = "/api/v3/brokerage/accounts";

/* ================= UTILS ================= */
function ensureDir() {
  fs.mkdirSync(DATA_DIR, { recursive: true });
}

function readJson(file, fallback) {
  try {
    if (!fs.existsSync(file)) return fallback;
    return JSON.parse(fs.readFileSync(file, "utf8"));
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

function sha256Hex(v) {
  return crypto.createHash("sha256").update(String(v)).digest("hex");
}

/* ================= STATE ================= */
function defaultState() {
  return {
    vault: {
      pin_hash: null,
      pin_set: false,
      session_token: null,
      session_expires_utc: null,
    },
    vault_keys: [],
  };
}

function loadState() {
  return readJson(STATE_FILE, defaultState());
}

function saveState(st) {
  writeJson(STATE_FILE, st);
}

/* ================= VAULT ================= */
function isVaultUnlocked(st) {
  return (
    st.vault.session_token &&
    new Date(st.vault.session_expires_utc).getTime() > Date.now()
  );
}

function requireVault(req, st) {
  const token = req.header("X-Vault-Token");
  if (!token) return { ok: false, error: "missing_vault_token", status: 401 };
  if (!isVaultUnlocked(st)) return { ok: false, error: "vault_locked", status: 401 };
  if (token !== st.vault.session_token)
    return { ok: false, error: "bad_vault_token", status: 401 };
  return { ok: true };
}

/* ================= CRYPTO ================= */
function aesKey() {
  if (!VAULT_MASTER_KEY) throw new Error("VAULT_MASTER_KEY not set");
  return crypto.createHash("sha256").update(VAULT_MASTER_KEY).digest();
}

function encrypt(text) {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", aesKey(), iv);
  const data = Buffer.concat([cipher.update(text), cipher.final()]);
  return {
    iv: iv.toString("base64"),
    tag: cipher.getAuthTag().toString("base64"),
    data: data.toString("base64"),
  };
}

function decrypt(enc) {
  const decipher = crypto.createDecipheriv(
    "aes-256-gcm",
    aesKey(),
    Buffer.from(enc.iv, "base64")
  );
  decipher.setAuthTag(Buffer.from(enc.tag, "base64"));
  return Buffer.concat([
    decipher.update(Buffer.from(enc.data, "base64")),
    decipher.final(),
  ]).toString();
}

/* ================= COINBASE ================= */
function buildCoinbaseJwt(keyName, pem, method, path) {
  const now = Math.floor(Date.now() / 1000);
  const uri = `${method} ${COINBASE_HOST}${path}`;

  const cleanPem = pem.includes("\\n") ? pem.replace(/\\n/g, "\n") : pem;
  const fixedPem = cleanPem.endsWith("\n") ? cleanPem : cleanPem + "\n";

  return jwt.sign(
    {
      sub: keyName,
      iss: "cdp",
      nbf: now,
      exp: now + 120,
      uri,
    },
    fixedPem,
    {
      algorithm: "ES256",
      header: {
        kid: keyName,
        nonce: crypto.randomBytes(16).toString("hex"),
      },
    }
  );
}

/* ================= MIDDLEWARE ================= */
app.use(express.json({ limit: "2mb" }));
app.use(cors({ origin: CORS_ORIGIN === "*" ? true : CORS_ORIGIN }));

/* ================= ROUTES ================= */
app.get("/health", (_req, res) =>
  res.json({ ok: true, service: "crypto-ai-api", time_utc: nowUtc() })
);

/* ---------- VAULT ---------- */
app.post("/vault/set-pin", (req, res) => {
  const pin = String(req.body?.pin || "");
  if (pin.length < 4) return res.status(400).json({ ok: false });
  const st = loadState();
  st.vault.pin_hash = sha256Hex(pin);
  st.vault.pin_set = true;
  saveState(st);
  res.json({ ok: true });
});

app.post("/vault/unlock", (req, res) => {
  const pin = String(req.body?.pin || "");
  const st = loadState();
  if (sha256Hex(pin) !== st.vault.pin_hash)
    return res.status(401).json({ ok: false });
  st.vault.session_token = crypto.randomBytes(16).toString("hex");
  st.vault.session_expires_utc = new Date(
    Date.now() + VAULT_TTL_SECONDS * 1000
  ).toISOString();
  saveState(st);
  res.json({
    ok: true,
    token: st.vault.session_token,
    ttl_sec: VAULT_TTL_SECONDS,
  });
});

app.get("/vault/status", (_req, res) => {
  const st = loadState();
  res.json({
    ok: true,
    unlocked: isVaultUnlocked(st),
  });
});

/* ---------- STORE KEY ---------- */
app.post("/vault/keys", (req, res) => {
  const st = loadState();
  const chk = requireVault(req, st);
  if (!chk.ok) return res.status(chk.status).json(chk);

  const { name, api_key, api_secret } = req.body;
  const enc = encrypt(JSON.stringify({ api_key, api_secret }));
  st.vault_keys.push({ id: crypto.randomBytes(8).toString("hex"), name, enc });
  saveState(st);
  res.json({ ok: true, name });
});

app.get("/vault/keys", (req, res) => {
  const st = loadState();
  const chk = requireVault(req, st);
  if (!chk.ok) return res.status(chk.status).json(chk);
  res.json({
    ok: true,
    keys: st.vault_keys.map((k) => ({ id: k.id, name: k.name })),
  });
});

/* ---------- COINBASE ---------- */
app.get("/coinbase/ping", async (req, res) => {
  const st = loadState();
  const chk = requireVault(req, st);
  if (!chk.ok) return res.status(chk.status).json(chk);

  const k = st.vault_keys.find((x) => x.name === "coinbase_main");
  if (!k) return res.status(404).json({ ok: false });

  const { api_key, api_secret } = JSON.parse(decrypt(k.enc));
  const jwtToken = buildCoinbaseJwt(
    api_key,
    api_secret,
    "GET",
    COINBASE_ACCOUNTS_PATH
  );

  const r = await fetch(COINBASE_BASE + COINBASE_ACCOUNTS_PATH, {
    headers: { Authorization: `Bearer ${jwtToken}` },
  });

  if (!r.ok)
    return res
      .status(502)
      .json({ ok: false, status: r.status, detail: await r.text() });

  res.json({ ok: true, coinbase: "authenticated" });
});

/* ================= START ================= */
app.listen(PORT, "0.0.0.0", () => {
  ensureDir();
  console.log(`crypto-ai-api running on :${PORT}`);
});

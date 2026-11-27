/**
 * Brenvel Solar — Telegram bot on a Cloudflare Worker (instant webhook).
 *
 * Live data source: the Worker fetches an AES-GCM-encrypted `status.enc` that
 * GitHub Actions publishes to the public `bot-state` branch every ~15 min, and
 * decrypts it with DATA_KEY (= the dashboard password). So the public branch
 * carries no plaintext fleet data, and no cross-system secret / KV write is
 * needed. Subscribers live in KV (Worker-managed).
 *
 * Inbound (webhook), instant:
 *   - password gate: a chat sends BOT_PASSWORD once to subscribe;
 *   - "/status" or the "📊 Статус станцій" button -> current fleet state.
 *
 * Bindings (wrangler.toml / secrets):
 *   SOLAR_STATE   KV namespace (subscribers)
 *   DATA_URL      var: raw URL of the encrypted status file
 *   DATA_KEY      secret: password to decrypt it
 *   BOT_TOKEN     secret: Telegram bot token
 *   BOT_PASSWORD  secret: access password to subscribe
 *   WEBHOOK_SECRET secret: Telegram webhook secret token
 */

const MENU = { keyboard: [[{ text: "📊 Статус станцій" }]], resize_keyboard: true };
const PASSWORD_PROMPT =
  "🔒 Вітаю! Щоб отримувати сповіщення <b>Brenvel Solar</b>, надішліть пароль доступу одним повідомленням.";
const WELCOME =
  "✅ Доступ надано — ви підписані на сповіщення <b>Brenvel Solar</b>.\n" +
  "Кнопка «📊 Статус станцій» нижче — поточний стан усіх станцій будь-коли.";

let _cache = { at: 0, data: null };

export default {
  async fetch(request, env) {
    const u = new URL(request.url);
    if (request.method === "GET") {
      if (u.pathname === "/selftest") {
        const snap = await getSnapshot(env);
        return new Response("ok stations=" + (snap ? snap.stations.length : "NULL") +
          " updated=" + (snap ? snap.updated : "-"), { status: 200 });
      }
      return new Response("Brenvel Solar bot is running.", { status: 200 });
    }
    if (request.method !== "POST") return new Response("ok");
    if (env.WEBHOOK_SECRET &&
        request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    let update;
    try { update = await request.json(); } catch { return new Response("ok"); }
    try { await handleUpdate(update, env); } catch (e) { console.log("handleUpdate error:", e); }
    return new Response("ok");
  },
};

async function handleUpdate(update, env) {
  const msg = update.message || update.edited_message || update.channel_post;
  if (!msg || !msg.chat || msg.chat.id == null) return;
  const chatId = msg.chat.id;
  const text = ((msg.text || msg.caption || "") + "").trim();

  const subs = await getJSON(env, "subscribers", []);
  if (!subs.includes(chatId)) {
    const pw = (env.BOT_PASSWORD || "").trim();
    if (pw && text === pw) {
      subs.push(chatId);
      await env.SOLAR_STATE.put("subscribers", JSON.stringify([...new Set(subs)]));
      await send(env, chatId, WELCOME);
    } else {
      await send(env, chatId, PASSWORD_PROMPT);
    }
    return;
  }
  await send(env, chatId, await renderStatus(env));
}

async function renderStatus(env) {
  const snap = await getSnapshot(env);
  if (!snap || !Array.isArray(snap.stations)) {
    return "⏳ Дані ще готуються — спробуй за кілька хвилин.";
  }
  const online = [], warn = [], offline = [];
  for (const s of snap.stations) {
    if (s.status === "connected") online.push(s);
    else if (s.status === "trouble") warn.push(s);
    else offline.push(s);
  }
  let out = `📡 <b>Стан станцій</b>  <i>(${esc(snap.updated || "")})</i>`;
  for (const s of online)  out += `\n🟢 ${esc(s.name)} — ${fkw(s.now_kw)}`;
  for (const s of warn)    out += `\n🟡 ${esc(s.name)} — ${fkw(s.now_kw)} · аварія`;
  for (const s of offline) out += `\n🔴 ${esc(s.name)} — офлайн`;
  const t = snap.totals || {};
  out += `\n\n⚡ Зараз разом: <b>${fkw(t.now_kw)}</b>`;
  out += `\n☀️ Сьогодні: ${fkwh(t.today_pv)} · ${feur(t.today_rev)}`;
  return out;
}

// ---- live data: fetch + decrypt the published status.enc ------------------
async function getSnapshot(env) {
  const now = Date.now();
  if (_cache.data && now - _cache.at < 60000) return _cache.data;
  try {
    const r = await fetch(env.DATA_URL + "?t=" + Math.floor(now / 60000),
      { cf: { cacheTtl: 30 }, headers: { "cache-control": "no-cache" } });
    if (!r.ok) return _cache.data;
    const blob = (await r.text()).trim();
    const snap = JSON.parse(await decrypt(blob, env.DATA_KEY));
    _cache = { at: now, data: snap };
    return snap;
  } catch (e) {
    console.log("getSnapshot error:", e);
    return _cache.data;
  }
}

async function decrypt(b64, password) {
  const raw = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  const salt = raw.slice(0, 16), iv = raw.slice(16, 28), ct = raw.slice(28);
  const km = await crypto.subtle.importKey("raw", new TextEncoder().encode(password),
    "PBKDF2", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey(
    { name: "PBKDF2", salt, iterations: 100000, hash: "SHA-256" }, km,
    { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
  const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, key, ct);
  return new TextDecoder().decode(pt);
}

// ---- helpers --------------------------------------------------------------
async function getJSON(env, key, dflt) {
  const raw = await env.SOLAR_STATE.get(key);
  if (!raw) return dflt;
  try { return JSON.parse(raw); } catch { return dflt; }
}

async function send(env, chatId, text) {
  const r = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/sendMessage`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text, parse_mode: "HTML",
      disable_web_page_preview: true, reply_markup: MENU }),
  });
  if (!r.ok) console.log("sendMessage failed", r.status, await r.text());
}

function esc(s) {
  return (s + "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function fkw(kw) {
  kw = Number(kw) || 0;
  return kw >= 1000 ? (kw / 1000).toFixed(2).replace(".", ",") + " МВт" : kw.toFixed(kw < 100 ? 1 : 0) + " кВт";
}
function fkwh(k) {
  k = Number(k) || 0;
  return k >= 1000 ? (k / 1000).toFixed(1).replace(".", ",") + " МВт·год" : Math.round(k) + " кВт·год";
}
function feur(e) {
  e = Math.round(Number(e) || 0);
  return "€" + (e + "").replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

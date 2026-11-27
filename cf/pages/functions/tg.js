/**
 * /tg — Telegram webhook for the Brenvel Solar bot (instant replies).
 *
 *  - password gate: a chat sends BOT_PASSWORD once to subscribe (stored in KV);
 *  - "/status" or the "📊 Статус станцій" button -> current fleet state,
 *    decrypted from status.enc that GitHub Actions publishes every ~5 min.
 *
 * Alerts and daily/weekly/monthly reports are sent by CI (notify.py), not here.
 *
 * Bindings (cf/pages/wrangler.toml + `wrangler pages secret put`):
 *   SOLAR_STATE     KV namespace (key "subscribers")
 *   OWNER_CHATS     var: comma-separated chat ids that are always allowed
 *   BOT_TOKEN       secret: Telegram bot token
 *   BOT_PASSWORD    secret: access password to subscribe
 *   WEBHOOK_SECRET  secret: must match the webhook's secret_token
 *   DATA_KEY        secret: password to decrypt status.enc
 */
import { fetchDecrypted } from "../lib/crypto.js";

const MENU = { keyboard: [[{ text: "📊 Статус станцій" }]], resize_keyboard: true };
const PASSWORD_PROMPT =
  "🔒 Вітаю! Щоб отримувати сповіщення <b>Brenvel Solar</b>, надішліть пароль доступу одним повідомленням.";
const WELCOME =
  "✅ Доступ надано — ви підписані на сповіщення <b>Brenvel Solar</b>.\n" +
  "Кнопка «📊 Статус станцій» нижче — поточний стан усіх станцій будь-коли.";

let snapCache = { at: 0, data: null };

// health check (no fleet data exposed): data decrypts + KV binding present
export async function onRequestGet({ env }) {
  if (!env.SOLAR_STATE || typeof env.SOLAR_STATE.get !== "function") {
    return new Response("no kv binding", { status: 503 });
  }
  const snap = await getSnapshot(env);
  return new Response(snap ? "ok" : "no data", { status: snap ? 200 : 503 });
}

export async function onRequestPost({ request, env }) {
  if (!env.WEBHOOK_SECRET ||
      request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
    return new Response("forbidden", { status: 403 });
  }
  let update;
  try { update = await request.json(); } catch { return new Response("ok"); }
  try { await handleUpdate(update, env); } catch (e) { console.log("handleUpdate error:", e); }
  return new Response("ok");   // always 200 so Telegram doesn't retry-storm
}

async function handleUpdate(update, env) {
  const msg = update.message || update.edited_message || update.channel_post;
  if (!msg || !msg.chat || msg.chat.id == null) return;
  const chatId = msg.chat.id;
  const text = ((msg.text || msg.caption || "") + "").trim();

  const subs = await getJSON(env, "subscribers", []);
  const owners = (env.OWNER_CHATS || "").split(",").map((s) => parseInt(s.trim(), 10)).filter((n) => !isNaN(n));
  if (!subs.includes(chatId) && !owners.includes(chatId)) {
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

async function getSnapshot(env) {
  const now = Date.now();
  if (snapCache.data && now - snapCache.at < 60000) return snapCache.data;
  try {
    const snap = JSON.parse(await fetchDecrypted("status.enc", env.DATA_KEY));
    snapCache = { at: now, data: snap };
    return snap;
  } catch (e) {
    console.log("getSnapshot error:", e);
    return snapCache.data;
  }
}

async function getJSON(env, key, dflt) {
  try {
    const raw = await env.SOLAR_STATE.get(key);
    return raw ? JSON.parse(raw) : dflt;
  } catch { return dflt; }
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
  return "€" + (e + "").replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

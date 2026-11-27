/**
 * GET /data — live fleet data for the dashboard page (same origin, no CORS).
 *
 * GitHub Actions publishes an encrypted bundle (data.enc) every ~5 min; this
 * returns it decrypted, but only to a caller that sends the team password
 * (Pages secret DATA_KEY) in the `x-pw` header.
 */
import { fetchDecrypted } from "../lib/crypto.js";

// per-isolate cache: CI republishes every ~5 min, so 60 s costs no freshness
// and saves a GitHub fetch + PBKDF2 per page view
let cache = { at: 0, json: null };

export async function onRequestGet({ request, env }) {
  const pw = request.headers.get("x-pw") || "";
  if (!env.DATA_KEY || pw !== env.DATA_KEY) {
    return new Response("unauthorized", { status: 401 });
  }
  const ok = (json) => new Response(json, {
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" } });
  const now = Date.now();
  if (cache.json && now - cache.at < 60000) return ok(cache.json);
  try {
    const json = await fetchDecrypted("data.enc", env.DATA_KEY);
    cache = { at: now, json };
    return ok(json);
  } catch (e) {
    console.log("data error:", e);
    if (cache.json) return ok(cache.json);          // last good copy beats an error
    return new Response("data unavailable", { status: 502 });
  }
}

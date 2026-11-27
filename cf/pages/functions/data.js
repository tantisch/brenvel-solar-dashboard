/**
 * GET /data — live fleet data for the dashboard page (same origin, no CORS).
 *
 * GitHub Actions publishes an AES-256-GCM-encrypted bundle (data.enc) to the
 * public `bot-state` branch every ~5 min; this function fetches it, decrypts
 * it with DATA_KEY (= the team password, Pages secret) and returns the JSON,
 * but only to a caller that sends the same password in the `x-pw` header.
 */
const SRC = "https://raw.githubusercontent.com/tantisch/brenvel-solar-dashboard/bot-state/data.enc";

// decrypted bundle kept per isolate: CI republishes every ~5 min, so a 60 s
// cache costs nothing in freshness and saves a GitHub fetch + PBKDF2 per view
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
    const r = await fetch(SRC + "?t=" + now, {
      cf: { cacheTtl: 0 }, headers: { "cache-control": "no-cache" },
      signal: AbortSignal.timeout(12000) });
    if (!r.ok) throw new Error("upstream " + r.status);
    const json = await decrypt((await r.text()).trim(), env.DATA_KEY);
    cache = { at: now, json };
    return ok(json);
  } catch (e) {
    console.log("data error:", e);
    if (cache.json) return ok(cache.json);          // last good copy beats an error
    return new Response("data unavailable", { status: 502 });
  }
}

// format = base64(salt16 ‖ iv12 ‖ ciphertext+tag), PBKDF2-SHA256 100k (cryptoutil.py)
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

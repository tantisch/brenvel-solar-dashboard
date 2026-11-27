/**
 * Shared helpers for the Pages Functions: fetch an encrypted file published by
 * GitHub Actions on the public `bot-state` branch and decrypt it.
 *
 * Format (cryptoutil.py): base64(salt16 ‖ iv12 ‖ ciphertext+tag),
 * AES-256-GCM with a key from PBKDF2-HMAC-SHA256 (100k iterations).
 */
export const RAW = "https://raw.githubusercontent.com/tantisch/brenvel-solar-dashboard/bot-state/";

export async function decrypt(b64, password) {
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

/** Fetch RAW+file (bypassing caches) and return the decrypted text. */
export async function fetchDecrypted(file, password) {
  const r = await fetch(RAW + file + "?t=" + Date.now(), {
    cf: { cacheTtl: 0 }, headers: { "cache-control": "no-cache" },
    signal: AbortSignal.timeout(12000) });
  if (!r.ok) throw new Error("upstream " + r.status);
  return decrypt((await r.text()).trim(), password);
}

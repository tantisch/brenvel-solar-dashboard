"""
AES-256-GCM encryption compatible with the Cloudflare Worker (WebCrypto).

Format: base64( salt[16] || iv[12] || ciphertext+tag ), key = PBKDF2-HMAC-SHA256
(100k iters). Used to publish the live status snapshot encrypted with
DASHBOARD_PASSWORD, so the public `bot-state` branch carries no plaintext fleet
data; the Worker decrypts it with the same password (no new secret needed).

CLI:  DATA_PASSWORD=.. python cryptoutil.py enc <infile>   # -> base64 on stdout
"""
import os
import sys
import base64

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

ITERATIONS = 100000


def _key(password, salt):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS)
    return kdf.derive(password.encode("utf-8"))


def encrypt(plaintext, password):
    salt = os.urandom(16)
    iv = os.urandom(12)
    ct = AESGCM(_key(password, salt)).encrypt(iv, plaintext.encode("utf-8"), None)
    return base64.b64encode(salt + iv + ct).decode("ascii")


def decrypt(blob, password):
    raw = base64.b64decode(blob)
    salt, iv, ct = raw[:16], raw[16:28], raw[28:]
    return AESGCM(_key(password, salt)).decrypt(iv, ct, None).decode("utf-8")


if __name__ == "__main__":
    pw = (os.environ.get("DATA_PASSWORD") or os.environ.get("STATICRYPT_PASSWORD")
          or os.environ.get("DASHBOARD_PASSWORD") or "")
    if len(sys.argv) >= 3 and sys.argv[1] == "enc" and pw:
        sys.stdout.write(encrypt(open(sys.argv[2], encoding="utf-8").read(), pw))
    else:
        sys.stderr.write("usage: DATA_PASSWORD=.. python cryptoutil.py enc <infile>\n")
        sys.exit(2)

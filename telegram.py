"""
Minimal Telegram Bot API client (stdlib only) for the solar fleet bot.

No third-party dependencies — just urllib. Used by notify.py to deliver fault
alerts and periodic reports to a Telegram chat (private, group, or channel).

Env:
  TELEGRAM_BOT_TOKEN  - bot token from @BotFather
  TELEGRAM_CHAT_ID    - target chat id (user id, group id (negative), or @channel)
  TELEGRAM_DRY_RUN    - if "1"/"true"/"yes": print messages instead of sending

CLI (setup helpers):
  python telegram.py chatid   # print chat ids that have messaged the bot
  python telegram.py test     # send a test message to TELEGRAM_CHAT_ID
"""
import os
import re
import sys
import ssl
import json
import time
import urllib.request
import urllib.parse
import urllib.error

# Optional: load TELEGRAM_* from a local .env for convenience (no-op in CI /
# if python-dotenv isn't installed). Shell-provided env vars still win.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

API = "https://api.telegram.org/bot{token}/{method}"
_CTX = ssl.create_default_context()
# A real BotFather token is "<digits>:<base64-ish>" and ASCII-only.
_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")


def valid_token(t):
    return bool(t and _TOKEN_RE.match(t.strip()))


def _call(token, method, params, timeout=30):
    url = API.format(token=token, method=method)
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"User-Agent": "solar-bot"})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _dry():
    return os.environ.get("TELEGRAM_DRY_RUN", "").lower() in ("1", "true", "yes")


def _deliver(token, chat_id, text, parse_mode="HTML", disable_preview=True):
    """Deliver to one chat. Return 'ok', 'dead' (invalid/blocked chat — prune it),
    or 'fail' (transient — keep and retry next cycle)."""
    params = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": "true" if disable_preview else "false",
    }
    for _ in range(3):
        try:
            j = _call(token, "sendMessage", params)
            if j.get("ok"):
                return "ok"
            ra = (j.get("parameters") or {}).get("retry_after")
            if ra:
                time.sleep(min(int(ra) + 1, 30))
                continue
            print(f"   ! telegram not ok for {chat_id}: {str(j)[:120]}")
            return "dead"
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            ra = None
            try:
                ra = (json.loads(body).get("parameters") or {}).get("retry_after")
            except Exception:
                pass
            if e.code == 429 and ra:
                time.sleep(min(int(ra) + 1, 30))
                continue
            # 400 chat not found / 403 bot blocked or kicked => stop messaging it
            if e.code in (400, 403):
                print(f"   ! telegram {e.code} for {chat_id}: {body[:100]}")
                return "dead"
            print(f"   ! telegram HTTP {e.code}: {body[:120]}")
        except Exception as e:
            print(f"   ! telegram send failed for {chat_id}: {e}")
        time.sleep(2)
    return "fail"


def send(text, token=None, chat_id=None, parse_mode="HTML", disable_preview=True):
    """Send one message to one chat. True only on a confirmed send (False in
    dry-run / when token or chat is missing)."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID")
    if _dry() or not token or chat_id in (None, ""):
        why = "DRY_RUN" if _dry() else "no token/chat_id"
        print("-" * 56)
        print(f"[telegram {why} -> chat {chat_id if chat_id not in (None, '') else '?'}]\n{text}")
        print("-" * 56)
        return False
    return _deliver(token, chat_id, text, parse_mode, disable_preview) == "ok"


def broadcast(text, chat_ids, token=None, parse_mode="HTML", disable_preview=True):
    """Send the same message to many chats. Return (delivered_count, dead_ids),
    where dead_ids are chats the caller should drop (blocked bot / chat gone)."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    ids, seen = [], set()
    for c in chat_ids:                       # de-dup, keep order, drop blanks
        if c in (None, "") or c in seen:
            continue
        seen.add(c)
        ids.append(c)
    if _dry() or not token or not ids:
        why = "DRY_RUN" if _dry() else ("no token" if not token else "no recipients")
        print("-" * 56)
        print(f"[telegram {why} -> {len(ids)} chat(s)]\n{text}")
        print("-" * 56)
        return (0, [])
    delivered, dead = 0, []
    for cid in ids:
        st = _deliver(token, cid, text, parse_mode, disable_preview)
        if st == "ok":
            delivered += 1
        elif st == "dead":
            dead.append(cid)
        time.sleep(0.04)                     # stay well under Telegram's ~30 msg/s
    return (delivered, dead)


def get_subscriber_ids(token=None):
    """Chat ids that have messaged the bot recently (for auto-subscribe)."""
    return [cid for cid, _ in get_chat_ids(token)]


def poll_updates(token=None, offset=0):
    """Consume incoming messages exactly once via the getUpdates offset.
    Return (next_offset, [(chat_id, text), ...]). Passing the returned
    next_offset on the following call confirms (drops) the updates read here."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return (offset, [])
    params = {"timeout": 0, "allowed_updates": json.dumps(["message", "channel_post"])}
    if offset:
        params["offset"] = offset
    try:
        j = _call(token, "getUpdates", params)
    except urllib.error.HTTPError as e:
        print("   ! telegram getUpdates 401 — token rejected" if e.code == 401
              else f"   ! telegram getUpdates HTTP {e.code}")
        return (offset, [])
    except Exception as e:
        print(f"   ! telegram getUpdates failed: {e}")
        return (offset, [])
    out, nxt = [], offset
    for u in j.get("result", []):
        uid = u.get("update_id")
        if isinstance(uid, int) and uid >= nxt:
            nxt = uid + 1
        m = u.get("message") or u.get("channel_post")
        if not m:
            continue
        cid = (m.get("chat") or {}).get("id")
        if cid is None:
            continue
        out.append((cid, m.get("text") or m.get("caption") or ""))
    return (nxt, out)


def get_chat_ids(token=None):
    """Return [(chat_id, label)] for chats seen in recent updates (setup aid)."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return []
    try:
        j = _call(token, "getUpdates",
                  {"timeout": 0, "allowed_updates":
                      json.dumps(["message", "channel_post", "my_chat_member", "edited_message"])})
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("❌ Токен не підходить (401). Перевір, що скопіював його повністю з @BotFather.")
        else:
            print(f"getUpdates failed: HTTP {e.code}")
        return []
    except Exception as e:
        print(f"getUpdates failed: {e}")
        return []
    seen = {}
    for u in j.get("result", []):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            m = u.get(key)
            if not m:
                continue
            ch = m.get("chat") or {}
            cid = ch.get("id")
            if cid is None:
                continue
            label = (ch.get("title")
                     or " ".join(filter(None, [ch.get("first_name"), ch.get("last_name")]))
                     or ch.get("username") or ch.get("type") or "")
            seen[cid] = f"{label} [{ch.get('type')}]"
    return list(seen.items())


def _ask_token():
    """Use TELEGRAM_BOT_TOKEN if already valid, otherwise prompt to paste it."""
    t = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if valid_token(t):
        return t
    if not sys.stdin.isatty():
        return t
    try:
        t = input("\nВстав токен від @BotFather і натисни Enter:\n> ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    return t


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "chatid"

    if cmd in ("chatid", "setup"):
        token = _ask_token()
        if not valid_token(token):
            print("❌ Це не схоже на токен. Він має виглядати як 123456789:AAHxxxxxxxx...\n"
                  "   Створи бота в @BotFather (/newbot) і скопіюй токен ПОВНІСТЮ.")
            sys.exit(1)
        ids = get_chat_ids(token)
        if not ids and sys.stdin.isatty():
            try:
                input("\nТепер відкрий свого бота в Telegram і натисни Start "
                      "(або додай його в групу/канал і напиши там).\n"
                      "Коли зробиш — натисни Enter тут… ")
            except (EOFError, KeyboardInterrupt):
                pass
            ids = get_chat_ids(token)
        if not ids:
            print("\nПоки не бачу повідомлень боту. Напиши йому /start і запусти ще раз:  python3 telegram.py")
        else:
            print("\n✅ Ось що знайшов — візьми потрібний chat_id:")
            for cid, label in ids:
                print(f"   chat_id = {cid}    {label}")
            print("\nДалі збережи два секрети: TELEGRAM_BOT_TOKEN (токен) і TELEGRAM_CHAT_ID (цей id).")
    elif cmd == "test":
        ok = send("✅ <b>Brenvel Solar</b> — тестове повідомлення. Бот працює.",
                  token=_ask_token())
        print("sent" if ok else "not sent (dry-run, or token/chat missing / error)")
    else:
        print("usage: python3 telegram.py [chatid|test]")

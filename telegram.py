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
import sys
import ssl
import json
import time
import urllib.request
import urllib.parse
import urllib.error

API = "https://api.telegram.org/bot{token}/{method}"
_CTX = ssl.create_default_context()


def _call(token, method, params, timeout=30):
    url = API.format(token=token, method=method)
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"User-Agent": "solar-bot"})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _dry():
    return os.environ.get("TELEGRAM_DRY_RUN", "").lower() in ("1", "true", "yes")


def send(text, token=None, chat_id=None, parse_mode="HTML", disable_preview=True):
    """Send one message. Returns True only on a real, confirmed send.

    In dry-run (or with no token/chat configured) the message is printed and
    False is returned, so callers must treat dry-run state-advancement separately.
    """
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if _dry() or not token or not chat_id:
        why = "DRY_RUN" if _dry() else "no token/chat_id"
        print("-" * 56)
        print(f"[telegram {why} -> chat {chat_id or '?'}]\n{text}")
        print("-" * 56)
        return False
    params = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": "true" if disable_preview else "false",
    }
    for attempt in range(3):
        try:
            j = _call(token, "sendMessage", params)
            if j.get("ok"):
                return True
            ra = (j.get("parameters") or {}).get("retry_after")
            if ra:
                time.sleep(min(int(ra) + 1, 30))
                continue
            print(f"   ! telegram sendMessage not ok: {str(j)[:150]}")
            return False
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
            print(f"   ! telegram HTTP {e.code}: {body[:150]}")
            if 400 <= e.code < 500:   # 4xx (bad token/chat) won't fix on retry
                return False
        except Exception as e:
            print(f"   ! telegram send failed: {e}")
        time.sleep(2)
    return False


def get_chat_ids(token=None):
    """Return [(chat_id, label)] for chats seen in recent updates (setup aid)."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return []
    try:
        j = _call(token, "getUpdates",
                  {"timeout": 0, "allowed_updates":
                      json.dumps(["message", "channel_post", "my_chat_member", "edited_message"])})
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


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"
    if cmd == "chatid":
        ids = get_chat_ids()
        if not ids:
            print("Жодного чату не знайдено.\n"
                  "1) Відкрий Telegram, знайди свого бота за ім'ям і натисни Start (або додай "
                  "його в групу й напиши там будь-яке повідомлення).\n"
                  "2) Запусти цю команду ще раз — вона покаже chat_id.")
        else:
            print("Знайдені чати (візьми потрібний chat_id):")
            for cid, label in ids:
                print(f"  chat_id = {cid}    {label}")
    elif cmd == "test":
        ok = send("✅ <b>Brenvel Solar</b> — тестове повідомлення. Бот працює.")
        print("sent" if ok else "not sent (dry-run, or token/chat missing / error)")
    else:
        print("usage: python telegram.py [test|chatid]")

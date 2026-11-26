"""
Telegram notifier for the Brenvel solar fleet.

Reads output/data.json (produced by collect.py / build_dashboard.py) plus a small
persistent JSON state file, then:

  * ALERT  — a station drops to disconnected/trouble *during the day* (while the
             fleet is generating or it's 09:00-16:00), and RECOVERY when it comes
             back. Nightly inverter sleep (whole fleet offline) never alerts.
  * REPORT — daily / weekly / monthly generation + revenue digest when due.

The state file only stores each station's last health + the last date a report
was sent (never generation/revenue numbers), so it is safe to keep on a public
branch. Revenue mirrors the dashboard: pv_kWh x eur_mwh / 1000 from
config/prices.json (this module only *reads* that file — tariffs are never
changed here).

Usage:
    TELEGRAM_BOT_TOKEN=.. TELEGRAM_CHAT_ID=.. python notify.py <state.json>
    TELEGRAM_DRY_RUN=1 python notify.py <state.json>        # print, don't send

Env:
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID   delivery target
    TELEGRAM_DRY_RUN                         print instead of sending
    REPORT_HOUR    local hour 0-23 for day/week/month reports (default 21)
    DATA_FILE      path to data.json (default output/data.json)
"""
import os
import re
import sys
import json
import html
from datetime import datetime, timezone, timedelta

import telegram

HERE = os.path.dirname(os.path.abspath(__file__))
BAD = {"disconnected", "unknown", "trouble"}
DAY_FLEET_KW = 5.0            # fleet output above this => treat as daytime
REPORT_HOUR = int(os.environ.get("REPORT_HOUR", "21"))
DRY = os.environ.get("TELEGRAM_DRY_RUN", "").lower() in ("1", "true", "yes")

UK_MONTH = ["", "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
            "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень"]


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #
def _kyiv_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Kyiv"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=2)))   # EET fallback (winter)


def esc(s):
    return html.escape(str(s), quote=False)


def fmt_kwh(k):
    k = float(k or 0)
    if k >= 1000:
        return f"{k / 1000:.1f} МВт·год".replace(".", ",")
    return f"{k:.0f} кВт·год"


def fmt_eur(e):
    return "€" + f"{float(e or 0):,.0f}".replace(",", " ")


def fmt_uah(u):
    return f"{float(u or 0):,.0f}".replace(",", " ") + " ₴"


def _human_since(iso, now):
    if not iso:
        return ""
    try:
        t0 = datetime.fromisoformat(iso)
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=now.tzinfo)
        secs = int((now - t0).total_seconds())
    except Exception:
        return ""
    if secs < 60:
        return "менше хвилини"
    h, m = secs // 3600, (secs % 3600) // 60
    if h and m:
        return f"{h} год {m} хв"
    if h:
        return f"{h} год"
    return f"{m} хв"


def _in_range(label, start, end):
    try:
        d = datetime.strptime(str(label)[:10], "%Y-%m-%d").date()
    except Exception:
        return False
    return start <= d <= end


def today_pv(s):
    v = (s.get("today") or {}).get("pv")
    if v is None:
        v = s.get("today_kwh")
    return float(v or 0)


def fleet_now_kw(bundle):
    return sum(float(s.get("now_kw") or 0) for s in bundle["stations"])


# --------------------------------------------------------------------------- #
#  pricing (mirror of build_dashboard.apply_prices, read-only)
# --------------------------------------------------------------------------- #
def load_prices():
    try:
        d = json.load(open(os.path.join(HERE, "config", "prices.json"), encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def price_ctx(bundle, prices):
    names = prices.get("names", {}) or {}
    overrides = prices.get("stations", {}) or {}
    fx = float(prices.get("uah_per_eur", 52) or 52)

    defs, rated = {}, []
    for s in bundle["stations"]:
        yrs = s.get("yearly") or []
        pv = sum((y.get("pv") or 0) for y in yrs)
        rev = sum((y.get("rev") or 0) for y in yrs)
        d = round(rev / pv * 1000, 1) if pv > 0 and rev > 0 else None
        defs[s["name"]] = d
        if d:
            rated.append(d)
    fleet_def = round(sum(rated) / len(rated), 1) if rated else 120.0

    disp, eur_mwh = {}, {}
    for s in bundle["stations"]:
        nm = s["name"]
        dn = names.get(nm)
        disp[nm] = dn.strip() if isinstance(dn, str) and dn.strip() else nm
        ov = overrides.get(nm) or {}
        e = ov.get("eur_mwh")
        eur_mwh[nm] = float(e) if isinstance(e, (int, float)) and e > 0 else (defs.get(nm) or fleet_def)
    return {"disp": disp, "eur_mwh": eur_mwh, "fx": fx}


def income_eur(name, pv_kwh, ctx):
    return (pv_kwh or 0) * ctx["eur_mwh"].get(name, 0) / 1000.0


# --------------------------------------------------------------------------- #
#  alerts
# --------------------------------------------------------------------------- #
def compute_alerts(prev, bundle, ctx, now, emit=True):
    """Pure: return (lines, entries). `entries` is the new per-station state map;
    callers persist it only once the alert message is actually delivered."""
    active = fleet_now_kw(bundle) > DAY_FLEET_KW or (9 <= now.hour < 16)
    lines, entries = [], {}
    for s in bundle["stations"]:
        nm = s["name"]
        name = ctx["disp"].get(nm, nm)
        status = s.get("status") or "unknown"
        bad = status in BAD
        p = prev.get(nm) or {}
        fault = bool(p.get("fault"))
        since = p.get("since")

        if bad and active and not fault:
            fault, since = True, now.isoformat(timespec="seconds")
            if emit:
                icon = "🟡" if status == "trouble" else "🔴"
                word = "попередження" if status == "trouble" else "немає зв'язку"
                lines.append(f"{icon} <b>{esc(name)}</b> — {word} (о {now:%H:%M})")
        elif (not bad) and fault:
            if emit:
                dur = _human_since(since, now)
                tail = f" (був офлайн {dur})" if dur else ""
                lines.append(f"🟢 <b>{esc(name)}</b> — знову онлайн{tail}")
            fault, since = False, None

        entries[nm] = {"fault": fault, "status": status, "since": since}
    return lines, entries


# --------------------------------------------------------------------------- #
#  reports
# --------------------------------------------------------------------------- #
def _rows(bundle, ctx, pv_of):
    rows, tp, tr = [], 0.0, 0.0
    for s in bundle["stations"]:
        pv = pv_of(s)
        if pv <= 0:
            continue
        rev = income_eur(s["name"], pv, ctx)
        rows.append((ctx["disp"].get(s["name"], s["name"]), pv, rev))
        tp += pv
        tr += rev
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows, tp, tr


def _report(title, subtitle, rows, tp, tr, fx, extra=None):
    out = [f"📊 <b>{esc(title)}</b>", esc(subtitle), ""]
    if rows:
        out += [f"• {esc(n)}: {fmt_kwh(pv)} · {fmt_eur(rev)}" for n, pv, rev in rows]
    else:
        out.append("<i>немає даних про виробіток за період</i>")
    out += ["", f"⚡ <b>Разом:</b> {fmt_kwh(tp)} · {fmt_eur(tr)} ({fmt_uah(tr * fx)})"]
    if extra:
        out += ["", extra]
    return "\n".join(out)


def due_reports(bundle, state, ctx, now):
    """Return [(state_marker, value, message)] for every report currently due."""
    out, fx = [], ctx["fx"]
    if now.hour < REPORT_HOUR:
        return out

    # DAILY -------------------------------------------------------------------
    today = now.strftime("%Y-%m-%d")
    if state.get("last_daily") != today:
        rows, tp, tr = _rows(bundle, ctx, today_pv)
        down = [ctx["disp"].get(s["name"], s["name"])
                for s in bundle["stations"] if (s.get("status") in BAD)]
        extra = ("⚠️ Зараз не на зв'язку: " + ", ".join(esc(d) for d in down)) if down else None
        out.append(("last_daily", today,
                    _report("Звіт за день", now.strftime("%d.%m.%Y"), rows, tp, tr, fx, extra)))

    iso = now.isocalendar()

    # WEEKLY (Mondays) — previous Mon..Sun --------------------------------------
    wkey = f"{iso.year}-W{iso.week:02d}"
    if iso.weekday == 1 and state.get("last_weekly") != wkey:
        start = (now - timedelta(days=7)).date()
        end = (now - timedelta(days=1)).date()
        rows, tp, tr = _rows(bundle, ctx, lambda s: sum(
            (d.get("pv") or 0) for d in (s.get("daily") or []) if _in_range(d.get("label"), start, end)))
        out.append(("last_weekly", wkey,
                    _report("Тижневий звіт", f"{start:%d.%m} – {end:%d.%m.%Y}", rows, tp, tr, fx)))

    # MONTHLY (1st) — previous calendar month ----------------------------------
    if now.day == 1:
        prev = now.replace(day=1) - timedelta(days=1)
        mkey = f"{prev.year}-{prev.month:02d}"
        if state.get("last_monthly") != mkey:
            rows, tp, tr = _rows(bundle, ctx, lambda s: sum(
                (m.get("pv") or 0) for m in (s.get("monthly") or []) if str(m.get("label")) == mkey))
            out.append(("last_monthly", mkey,
                        _report("Місячний звіт", f"{UK_MONTH[prev.month]} {prev.year}", rows, tp, tr, fx)))
    return out


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def _save(path, state):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1, sort_keys=True)


def _norm_chat(c):
    """Normalize a chat id so '123' and 123 don't double-send; keep @channel strings."""
    if c is None:
        return None
    if isinstance(c, int):
        return c
    s = str(c).strip()
    if not s:
        return None
    return int(s) if re.fullmatch(r"-?\d+", s) else s


WELCOME = ("✅ Ви підписані на сповіщення <b>Brenvel Solar</b>.\n"
           "Надсилатиму аварії станцій (вдень) та звіти — день, тиждень, місяць.")


def main(state_path):
    data_file = os.environ.get("DATA_FILE", os.path.join(HERE, "output", "data.json"))
    try:
        bundle = json.load(open(data_file, encoding="utf-8"))
    except Exception as e:
        print(f"notify: cannot read {data_file}: {e}")
        return
    if not bundle.get("stations"):
        print("notify: no stations in data; skipping")
        return

    try:
        state = json.load(open(state_path, encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}

    now = _kyiv_now()
    ctx = price_ctx(bundle, load_prices())

    # ---- recipients: everyone who has messaged the bot (auto-subscribe) -------
    # plus an optional TELEGRAM_CHAT_ID "seed" that is always included.
    subs = {_norm_chat(c) for c in (state.get("subscribers") or [])}
    subs.discard(None)
    seed = _norm_chat(os.environ.get("TELEGRAM_CHAT_ID"))
    try:
        for cid in telegram.get_subscriber_ids():
            cid = _norm_chat(cid)
            if cid is not None and cid not in subs:
                subs.add(cid)
                telegram.send(WELCOME, chat_id=cid)   # greet each new subscriber
    except Exception as e:
        print(f"notify: subscriber refresh failed: {e}")
    state["subscribers"] = sorted(c for c in subs if isinstance(c, int))
    recipients = list(subs | ({seed} if seed is not None else set()))

    def broadcast(msg):
        if not recipients:
            telegram.broadcast(msg, [])            # prints a dry/no-recipients note
            return DRY
        delivered, dead = telegram.broadcast(msg, recipients)
        if dead:
            for d in dead:
                subs.discard(_norm_chat(d))
            state["subscribers"] = sorted(c for c in subs if isinstance(c, int))
        return bool(delivered) or DRY

    # First activation: seed baseline health silently, then stop (new subscribers
    # were already welcomed above). Alerts/reports resume next cycle — no burst.
    if not state.get("activated"):
        _, entries = compute_alerts({}, bundle, ctx, now, emit=False)
        state["stations"] = entries
        state["activated"] = True
        _save(state_path, state)
        print(f"notify: activated, baseline for {len(bundle['stations'])} stations, "
              f"{len(recipients)} recipient(s)")
        return

    sent = 0

    # ---- alerts (at-least-once: advance state only on delivery) ----
    lines, entries = compute_alerts(state.get("stations") or {}, bundle, ctx, now, emit=True)
    if not lines:
        state["stations"] = entries          # no change to announce; keep health fresh
    else:
        header = "🛰 <b>Статус станцій</b>\n" if len(lines) > 1 else ""
        if broadcast(header + "\n".join(lines)):
            state["stations"] = entries
            sent += 1
        # on failure: keep previous health so the alert retries next cycle

    # ---- reports ----
    for marker, value, msg in due_reports(bundle, state, ctx, now):
        if broadcast(msg):
            state[marker] = value
            sent += 1

    _save(state_path, state)
    print(f"notify: {len(recipients)} recipient(s), {len(lines)} alert line(s), "
          f"{sent} broadcast(s)" + (" [dry-run]" if DRY else ""))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python notify.py <state.json>")
        sys.exit(2)
    main(sys.argv[1])

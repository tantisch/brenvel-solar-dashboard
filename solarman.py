"""
Solarman Smart adapter (home.solarmanpv.com "maintain-s" web API).

The station Овруч lives on Solarman, a THIRD platform. Its web login is behind a
slider CAPTCHA (can't be automated), and the official OpenAPI needs an appId the
account doesn't have. So instead we ride a JWT captured once from a browser
session, stored as the SOLARMAN_TOKEN secret.

Solarman's browser *access* token only lives ~1 day, but the *refresh* token
lives ~6 months and its refresh grant is NOT captcha-gated. So SOLARMAN_TOKEN
should hold the refresh token (the JWT that carries an "ati" claim); this adapter
exchanges it for a fresh access token on every run via the OAuth token endpoint.
A raw access token is still accepted (used directly) for backward compatibility.
When the refresh token finally expires the Solarman block just drops (caught in
collect.py) until it is re-captured from the browser.

Endpoints (all under https://home.solarmanpv.com, Authorization: bearer <token>):
  POST /maintain-s/operating/station/search            {page,size}      -> stations (+ live summary)
  GET  /maintain-s/history/power/{id}/record           ?year&month&day  -> intraday curve (records[].generationPower W)
  GET  /maintain-s/history/power/{id}/stats/month      ?year&month      -> daily energy in a month (records[])
  GET  /maintain-s/history/power/{id}/stats/year       ?year            -> monthly energy in a year + year total
  GET  /maintain-s/history/power/{id}/stats/total                       -> lifetime total
"""
import base64
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import ssl
from datetime import datetime, timezone, timedelta


def _local_now(tzname):
    """Current time in the plant's timezone (so 'today' matches the plant's day)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tzname or "Europe/Kyiv"))
    except Exception:
        return datetime.now(timezone.utc) + timedelta(hours=3)  # Kyiv summer fallback

BASE = "https://home.solarmanpv.com"
_CTX = ssl.create_default_context()

# OAuth token endpoint + the fixed public web client (base64 of "test:test").
# Used to exchange a long-lived refresh token for a short-lived access token.
_OAUTH_TOKEN_PATH = "/oauth2-s/oauth/token"
_OAUTH_BASIC = "Basic dGVzdDp0ZXN0"


def _jwt_claims(jwt):
    """Best-effort decode of a JWT payload (no signature check)."""
    try:
        p = jwt.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}

# Manual nameplate overrides — Solarman's installedCapacity is a hand-entered
# config field that doesn't always reflect reality. Овруч (id 66298046): 2400 kW
# after the 2nd inverter's datalogger was added (the API still reports 2200).
CAPACITY_KW = {66298046: 2400}

# networkStatus -> dashboard status
_STATUS = {
    "ALL_ONLINE": "connected",
    "NORMAL": "connected",
    "PARTIAL_OFFLINE": "trouble",
    "ALL_OFFLINE": "disconnected",
}


class SolarmanClient:
    def __init__(self, token):
        raw = (token or "").strip()
        self._raw = raw
        # A refresh token carries an "ati" claim; a plain access token does not.
        self._is_refresh = bool(_jwt_claims(raw).get("ati"))
        # Resolved bearer used for maintain-s calls; if we already hold an
        # access token, use it directly, otherwise it's minted on first request.
        self._access = None if self._is_refresh else raw

    # ---- auth --------------------------------------------------------------
    def _ensure_token(self):
        """Return a usable access token, exchanging the refresh token if needed."""
        if self._access:
            return self._access
        data = urllib.parse.urlencode(
            {"grant_type": "refresh_token", "refresh_token": self._raw}).encode()
        req = urllib.request.Request(
            BASE + _OAUTH_TOKEN_PATH, data=data, method="POST",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Authorization": _OAUTH_BASIC})
        try:
            with urllib.request.urlopen(req, timeout=30, context=_CTX) as r:
                j = json.loads(r.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"Solarman refresh-token exchange rejected ({e.code}) — refresh SOLARMAN_TOKEN")
        except Exception as e:
            raise RuntimeError(f"Solarman token endpoint unreachable: {e}")
        at = j.get("access_token")
        if not at:
            raise RuntimeError(
                f"Solarman refresh returned no access_token ({str(j)[:100]}) — refresh SOLARMAN_TOKEN")
        self._access = at
        return at

    # ---- transport ---------------------------------------------------------
    def _req(self, method, path, params=None, body=None, retries=2, timeout=30):
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Authorization": "bearer " + self._ensure_token(),
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        last = None
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                    return json.loads(r.read().decode("utf-8", "ignore"))
            except urllib.error.HTTPError as e:
                # 401/403 => token dead; don't retry, surface it
                if e.code in (401, 403):
                    raise RuntimeError(f"Solarman token rejected ({e.code}) — refresh SOLARMAN_TOKEN")
                last = e
            except Exception as e:  # timeouts / transient
                last = e
            if attempt < retries:
                time.sleep(1.5)
        raise RuntimeError(f"Solarman {method} {path} failed: {last}")

    def _get(self, path, params=None, **kw):
        return self._req("GET", path, params=params, **kw)

    # ---- data --------------------------------------------------------------
    def get_stations(self):
        """Return the raw station summary objects (usually one: Овруч)."""
        r = self._req("POST", "/maintain-s/operating/station/search",
                      body={"page": 1, "size": 100})
        return r.get("data") or []

    def _day_curve(self, sid, y, m, d, tz_off_default=10800):
        """Intraday PV power curve for one day, padded to a full 24h 5-min grid."""
        try:
            r = self._get(f"/maintain-s/history/power/{sid}/record",
                          {"year": y, "month": m, "day": d}, retries=1, timeout=20)
        except Exception:
            return []
        slots = {}
        for rec in (r.get("records") or []):
            p = rec.get("generationPower")
            ts = rec.get("dateTime")
            if p is None or ts is None:
                continue
            off = rec.get("timeZoneOffset")
            off = off if off is not None else tz_off_default
            lt = time.gmtime(int(ts) + int(off))
            mm = (lt.tm_min // 5) * 5
            slots[f"{lt.tm_hour:02d}:{mm:02d}"] = round(p / 1000.0, 2)  # W -> kW
        if not slots:
            return []
        return [{"t": f"{hh:02d}:{mm:02d}", "kw": slots.get(f"{hh:02d}:{mm:02d}")}
                for hh in range(24) for mm in range(0, 60, 5)]

    def get_history(self, st, start_ts):
        """Build today_curve + daily/monthly/yearly (with revenue) for a station."""
        sid = st["id"]
        tz = st.get("regionTimezone") or "Europe/Kyiv"
        now = _local_now(tz)                       # plant-local "today"
        y0, m0, d0 = now.year, now.month, now.day

        out = {"today_curve": [], "daily": [], "monthly": [], "yearly": [],
               "total_kwh": None, "total_rev": None}

        # today intraday
        out["today_curve"] = self._day_curve(sid, y0, m0, d0)

        # start month/year of the plant (bounds the history we pull)
        try:
            st_lt = time.gmtime(int(start_ts)) if start_ts else now
            sy, sm = st_lt.tm_year, st_lt.tm_mon
        except Exception:
            sy, sm = y0, m0

        # daily: walk each month from start to now, collect daily records (cap 15 months)
        months, yy, mm = [], sy, sm
        while (yy, mm) <= (y0, m0) and len(months) < 15:
            months.append((yy, mm))
            mm += 1
            if mm > 12:
                mm, yy = 1, yy + 1
        daily = []
        for (yy, mm) in months:
            try:
                r = self._get(f"/maintain-s/history/power/{sid}/stats/month",
                              {"year": yy, "month": mm}, retries=1, timeout=20)
            except Exception:
                continue
            for rec in (r.get("records") or []):
                dd = rec.get("day")
                if not dd:
                    continue
                daily.append({"label": f"{rec['year']:04d}-{rec['month']:02d}-{dd:02d}",
                              "pv": rec.get("generationValue") or 0,
                              "rev": rec.get("incomeValue")})
        out["daily"] = daily

        # monthly + yearly: one stats/year call per year gives monthly records + the year total
        monthly, yearly = [], []
        for yy in range(sy, y0 + 1):
            try:
                r = self._get(f"/maintain-s/history/power/{sid}/stats/year",
                              {"year": yy}, retries=1, timeout=20)
            except Exception:
                continue
            for rec in (r.get("records") or []):
                monthly.append({"label": f"{rec['year']:04d}-{rec['month']:02d}",
                                "pv": rec.get("generationValue") or 0,
                                "rev": rec.get("incomeValue")})
            stt = r.get("statistics") or {}
            yearly.append({"label": f"{yy:04d}", "pv": stt.get("generationValue") or 0,
                           "rev": stt.get("incomeValue")})
        out["monthly"] = monthly
        out["yearly"] = yearly

        # lifetime total
        try:
            t = self._get(f"/maintain-s/history/power/{sid}/stats/total", retries=1, timeout=20)
            stt = t.get("statistics") or {}
            out["total_kwh"] = stt.get("generationValue")
            out["total_rev"] = stt.get("incomeValue")
        except Exception:
            pass
        return out

    def get_fleet(self):
        """High-level: normalized station records ready for the dashboard bundle."""
        out = []
        for st in self.get_stations():
            try:
                out.append(self._normalize(st))
            except Exception as e:
                print(f"   ! Solarman station {st.get('name')} failed: {e}")
        return out

    def _normalize(self, st):
        sid = st["id"]
        cap = CAPACITY_KW.get(sid) or st.get("installedCapacity") or 0   # kW (manual override wins)
        now_kw = round((st.get("generationPower") or 0) / 1000.0, 2)  # W -> kW
        today_kwh = st.get("generationValue") or 0
        hist = self.get_history(st, st.get("startOperatingTime"))
        total_kwh = hist["total_kwh"] or st.get("generationTotal") or 0
        total_rev = hist["total_rev"]
        eur_kwh = round(total_rev / total_kwh, 4) if (total_rev and total_kwh) else None
        status = _STATUS.get(st.get("networkStatus"), "unknown")
        if status == "connected" and st.get("warningStatus") not in (None, "NORMAL"):
            status = "trouble"
        rec = {
            "name": st.get("name") or "Solarman",
            "source": "solarman",
            "metered": False,
            "status": status,
            "nominal_kw": cap,
            "now_kw": now_kw,
            "today": {"metered": False, "pv": today_kwh},
            "today_kwh": today_kwh,
            "total_kwh": total_kwh,
            "today_rev": st.get("incomeValue"),
            "total_rev": total_rev,
            "eur_kwh": eur_kwh,
            "lat": st.get("locationLat"),
            "lon": st.get("locationLng"),
            "address": st.get("locationAddress"),
            "device_num": None,
            "today_curve": hist["today_curve"],
            "daily": hist["daily"],
            "monthly": hist["monthly"],
            "yearly": hist["yearly"],
            "alarms": [],
        }
        return rec

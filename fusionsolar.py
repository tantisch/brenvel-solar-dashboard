"""
Multi-region FusionSolar (Huawei) web client.

This account's stations are split across two FusionSolar regional data centres
(region002 -> region02eu5, region004 -> region04eu5). A normal login only sees
one region, so this client logs in, enumerates every region the account can
reach, and authenticates each one independently.

Auth flow (reverse-engineered from the FusionSolar login bundle):
  1. POST /unisso/v3/validateUser.action with empty `multiRegionName`
     -> errorCode 470 + respMultiRegionName = ['region002', 'region004']
  2. POST the same endpoint again with `multiRegionName` = the chosen region
     -> errorCode 470 + respMultiRegionName = ['-5', '/rest/dp/.../on-sso-credential-ready?ticket=ST-...']
  3. GET that ticket path on the eu5 gateway (NOT the regional host) and follow
     redirects -> sets the domain-wide `dp-session` cookie -> every regional
     host is now authenticated.
  4. keep-alive on the regional host returns the `roarand` CSRF token used for
     subsequent data calls.
"""
from __future__ import annotations

import time
from typing import Iterator, Optional

import requests
from fusion_solar_py.encryption import encrypt_password, get_secure_random

GATEWAY = "eu5"  # the login gateway subdomain
# The relative service the web app authenticates against.
SERVICE = "/unisess/v1/auth?service=%2Fnetecowebext%2Fhome%2Findex.html"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
)


class FusionSolarError(Exception):
    """Raised when the API returns an unexpected response."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def region_to_subdomain(region_code: str) -> str:
    """'region002' -> 'region02eu5' (the host that serves the data API)."""
    num = int(region_code.replace("region", ""))
    return f"region{num:02d}eu5"


class RegionSession:
    """An authenticated session bound to a single FusionSolar region."""

    def __init__(self, session: requests.Session, subdomain: str, region_code: str):
        self._session = session
        self.subdomain = subdomain
        self.region_code = region_code

    def _url(self, path: str) -> str:
        return f"https://{self.subdomain}.fusionsolar.huawei.com{path}"

    def get_stations(self, page_size: int = 100) -> list[dict]:
        """Return every PV station in this region (full raw objects)."""
        r = self._session.post(
            self._url("/rest/pvms/web/station/v1/station/station-list"),
            json={
                "curPage": 1, "pageSize": page_size, "gridConnectedTime": "",
                "queryTime": _now_ms(), "timeZone": 2,
                "sortId": "createTime", "sortDir": "DESC", "locale": "en_US",
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise FusionSolarError(f"station-list failed: {str(data)[:200]}")
        return data["data"]["list"]

    def get_region_kpi(self) -> dict:
        """Aggregate real-time KPI across all stations in this region."""
        r = self._session.get(
            self._url("/rest/pvms/web/station/v1/station/total-real-kpi"),
            params={"queryTime": _now_ms(), "timeZone": 2, "_": _now_ms()},
            timeout=20,
        )
        r.raise_for_status()
        return r.json().get("data", {})

    def get_station_kpi(self, station_dn: str) -> dict:
        """Real-time KPI (power, daily/month/year energy, income) for one station."""
        r = self._session.get(
            self._url("/rest/pvms/web/station/v1/overview/station-real-kpi"),
            params={"stationDn": station_dn, "clientTime": _now_ms(),
                    "timeZone": 2, "_": _now_ms()},
            timeout=20,
        )
        r.raise_for_status()
        return r.json().get("data", {})

    # -- rate-limit-aware request helper -------------------------------------
    def _request(self, method: str, path: str, **kwargs):
        """Issue a request, retrying with backoff on the API's 'flow control'
        (HTTP 429) rate limiter."""
        url = self._url(path)
        last = None
        for attempt in range(6):
            last = self._session.request(method, url, timeout=30, **kwargs)
            head = last.text[:60].lower()
            if last.status_code == 200 and "flow control" not in head:
                return last
            time.sleep(2 + attempt * 2)   # 2,4,6,8,10s backoff
        return last

    # -- rich per-station data -----------------------------------------------
    _NODATA = 1.0e308   # FusionSolar's "no value" sentinel (1.7976931348623157e308)

    def get_power_curve(self, inverter_dns: list) -> list:
        """Today's intraday PV power curve (kW), summed across a station's
        INVERTERS only (pass inverter DNs — never meters/sensors, whose 30014
        signal is grid power, not generation).

        Returns the full-day 5-minute timeline [{t:'HH:MM', kw:float|None}].
        Slots with no inverter telemetry (night gaps, future) are None so the
        chart leaves them blank rather than inventing a value."""
        grid = {}   # startTime -> {"sum":kW, "has":bool, "off":seconds}
        for dn in inverter_dns:
            r = self._request(
                "GET", "/rest/pvms/web/device/v1/device-history-data",
                params=[("signalIds", "30014"), ("deviceDn", dn),
                        ("date", _now_ms()), ("_", _now_ms())],
            )
            node = (r.json().get("data", {}) or {}).get("30014", {}) or {}
            for p in node.get("pmDataList", []) or []:
                st = p.get("startTime")
                if st is None:
                    continue
                g = grid.setdefault(st, {"sum": 0.0, "has": False,
                    "off": (p.get("timeZoneOffset", 0) + p.get("dstOffset", 0)) * 60})
                v = p.get("counterValue")
                if v is not None and float(v) < self._NODATA:
                    g["sum"] += float(v)
                    g["has"] = True
        out = []
        for st in sorted(grid):
            g = grid[st]
            hhmm = time.strftime("%H:%M", time.gmtime(st + g["off"]))
            out.append({"t": hhmm, "kw": round(g["sum"], 2) if g["has"] else None})
        return out

    def get_history(self, station_dn: str, stat_dim: int, t0_ms: int, t1_ms: int) -> list:
        """Energy history via the report endpoint.
        stat_dim: 4=daily, 5=monthly, 6=yearly. Returns [{label, kwh, rev}]."""
        body = {
            "currencyUnit": "EUR", "orderBy": "statTime", "page": 1, "pageSize": 500,
            "moList": [{"moType": 20801, "moString": station_dn}],
            "counterIDs": ["productPower", "onGridPower", "powerProfit"],
            "sort": "asc", "statDim": stat_dim, "statTime": t0_ms, "statEndTime": t1_ms,
            "statType": "1", "station": "0", "timeZone": 3, "timeZoneStr": "Europe/Kiev",
        }
        r = self._request("POST", "/rest/pvms/web/report/v1/station/station-kpi-list", json=body)
        d = r.json().get("data")
        rows = d.get("list") if isinstance(d, dict) else d
        out = []
        for x in (rows or []):
            if x.get("productPower") is None:
                continue
            out.append({
                "label": x.get("fmtCollectTimeStr"),
                "kwh": round(float(x.get("productPower") or 0), 1),
                "rev": round(float(x.get("powerProfit") or 0), 2),
            })
        out.sort(key=lambda r: r["label"])
        return out

    def get_inverters(self, station_dn: str) -> list:
        """List inverters (and similar devices) under a station."""
        r = self._request(
            "GET", "/rest/neteco/web/config/device/v1/device-list",
            params={"conditionParams.parentDn": station_dn,
                    "conditionParams.mocTypes": "20814,20815,20816,20819,20822,50017,60066,60014,60015,23037",
                    "_": _now_ms()},
        )
        data = r.json().get("data", []) or []
        return [{"dn": d.get("dn"), "name": d.get("name"), "type": d.get("mocTypeName")}
                for d in data]

    def get_alarms(self, station_dn: str) -> list:
        """Active alarms for a station."""
        r = self._request(
            "POST", "/rest/pvms/fm/v1/query",
            json={"dataType": "CURRENT", "domainType": "OC_SOLAR",
                  "pageNo": 1, "pageSize": 20, "nativeMeDn": station_dn},
        )
        hits = (r.json().get("data", {}) or {}).get("hits", []) or []
        out = []
        for h in hits:
            out.append({
                "name": h.get("alarmName") or h.get("name") or "Alarm",
                "severity": h.get("severity") or h.get("level"),
                "device": h.get("meName") or h.get("deviceName"),
                "time": h.get("occurTime") or h.get("raisedTime") or h.get("createTime"),
            })
        return out


class FusionSolarClient:
    """Logs into FusionSolar and yields an authenticated session per region."""

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password

    # -- low level -----------------------------------------------------------
    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.headers["User-Agent"] = USER_AGENT
        return s

    def _validate_user(self, session: requests.Session, multi_region: str = ""):
        """One call to the v3 validateUser endpoint (RSA-encrypted password)."""
        key_data = session.get(
            f"https://{GATEWAY}.fusionsolar.huawei.com/unisso/pubkey", timeout=30
        ).json()
        enc_pw = encrypt_password(key_data=key_data, password=self.password)
        return session.post(
            f"https://{GATEWAY}.fusionsolar.huawei.com/unisso/v3/validateUser.action",
            params={"timeStamp": key_data["timeStamp"],
                    "nonce": get_secure_random(), "service": SERVICE},
            json={"organizationName": "", "username": self.username,
                  "password": enc_pw, "verifycode": "",
                  "multiRegionName": multi_region},
            timeout=30,
        ).json()

    # -- public API ----------------------------------------------------------
    def list_regions(self) -> list[str]:
        """Return the region codes the account can access, e.g. ['region002', 'region004'].

        Single-region accounts return [] (no region selection needed).
        """
        resp = self._validate_user(self._new_session())
        if str(resp.get("errorCode")) == "470":
            return list(resp.get("respMultiRegionName", []))
        return []

    def authenticate(self, region_code: str) -> RegionSession:
        """Fully authenticate one region and return a ready-to-use RegionSession."""
        subdomain = region_to_subdomain(region_code)
        session = self._new_session()

        self._validate_user(session)                       # -> 470 + region list
        resp = self._validate_user(session, region_code)   # -> ticket path
        parts = resp.get("respMultiRegionName", [])
        if len(parts) < 2 or "ticket=" not in str(parts[1]):
            raise FusionSolarError(
                f"region selection for {region_code} did not return a ticket: {parts}"
            )
        ticket_path = parts[1]

        # Redeem the CAS ticket on the gateway -> sets domain-wide dp-session cookie.
        session.get(f"https://{GATEWAY}.fusionsolar.huawei.com{ticket_path}",
                    timeout=30, allow_redirects=True)

        # keep-alive -> roarand CSRF token required by data endpoints.
        ka = session.get(
            f"https://{subdomain}.fusionsolar.huawei.com/rest/dpcloud/auth/v1/keep-alive",
            timeout=20,
        ).json()
        if ka.get("code") != 0 or not ka.get("payload"):
            raise FusionSolarError(
                f"authentication for {region_code} failed (keep-alive: {ka.get('message')})"
            )
        session.headers["roarand"] = ka["payload"]
        return RegionSession(session, subdomain, region_code)

    def iter_regions(self) -> Iterator[RegionSession]:
        """Yield an authenticated RegionSession for every region on the account."""
        regions = self.list_regions()
        if not regions:
            # single-region account: region002 default still works as the data host
            raise FusionSolarError(
                "No multi-region list returned; this client targets multi-region accounts."
            )
        for code in regions:
            yield self.authenticate(code)

    def get_all_stations(self) -> list[dict]:
        """Every station across every region, each annotated with its region/host."""
        stations: list[dict] = []
        for region in self.iter_regions():
            for st in region.get_stations():
                st["_region"] = region.region_code
                st["_subdomain"] = region.subdomain
                stations.append(st)
        return stations

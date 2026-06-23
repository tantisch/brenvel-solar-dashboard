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

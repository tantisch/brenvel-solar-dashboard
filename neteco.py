"""
Client for the Photomate NetEco 1000S platform (neteco.photomate.eu) — a
separate, older Huawei monitoring system that hosts two larger plants
(SES Krolovets, SES Jornava).

It is NOT the FusionSolar API: login is a Struts form with a CSRF token that
ROTATES after login, plaintext password over HTTPS, no captcha on first login.
Data comes from `sunmonitorjson!queryPlantListInfo.action` (live plant list).
Detailed daily/monthly history would need the heavy PM report-builder API, so
this adapter returns the reliable live snapshot only.
"""
import re
import html
import requests

BASE = "https://neteco.photomate.eu/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36")

# nice display names for the known plants (fallback: cleaned plantName)
PLANT_NAMES = {100109: "Кролевець", 100519: "Жорнава"}


def _unescape(s: str) -> str:
    """Decode NetEco's \\xNN escapes and HTML entities."""
    if not s:
        return ""
    s = re.sub(r"\\x([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), s)
    return html.unescape(s).strip()


def _num(s, d=0.0):
    try:
        m = re.search(r"-?[\d.]+(?:[eE][+-]?\d+)?", str(s))
        return float(m.group()) if m else d
    except (TypeError, ValueError):
        return d


class NetEcoError(Exception):
    pass


class NetEcoClient:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self._csrf_header = None

    def login(self):
        html_page = self.s.get(BASE + "index.action", timeout=30).text
        token = re.search(r'name="_csrf"\s+content="([^"]*)"', html_page)
        header = re.search(r'name="_csrf_header"\s+content="([^"]*)"', html_page)
        if not token or not header:
            raise NetEcoError("could not read login CSRF token")
        self.s.headers[header.group(1)] = token.group(1)
        r = self.s.post(
            BASE + "security!login.action",
            data={"userName": self.username, "password": self.password,
                  "dateTime": "0", "veryCode": "", "webLang": "en_US"},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": BASE + "index.action"},
            timeout=30,
        )
        if r.json().get("retMsg") != "op.successfully":
            raise NetEcoError(f"login failed: {r.text[:120]}")
        # complete login, then refresh the (rotated) CSRF token from an
        # authenticated page so data calls are accepted
        self.s.get(BASE + "securitys!tologin.action", timeout=30)
        ov = self.s.get(BASE + "overviewAction!toPvPlantOverviewMain.action", timeout=30).text
        nt = re.search(r'name="_csrf"\s+content="([^"]*)"', ov)
        nh = re.search(r'name="_csrf_header"\s+content="([^"]*)"', ov)
        if nt and nh:
            self.s.headers.pop(header.group(1), None)
            self.s.headers[nh.group(1)] = nt.group(1)
            self._csrf_header = nh.group(1)
        return self

    def get_plants(self) -> list:
        """Return the live plant list as normalized records."""
        r = self.s.post(
            BASE + "sunmonitorjson!queryPlantListInfo.action",
            data={"groupName": "", "_search": "false", "page": 1, "rows": 100,
                  "sidx": "", "sord": "asc"},
            headers={"X-Requested-With": "XMLHttpRequest",
                     "Referer": BASE + "overviewAction!toPvPlantOverviewMain.action"},
            timeout=30,
        )
        data = r.json()
        out = []
        for p in data.get("plantDetailInfos", []):
            sn = p.get("plantSn")
            name = PLANT_NAMES.get(sn) or _unescape(p.get("plantName"))
            status = "connected" if str(p.get("status", "")).lower() == "normal" else "trouble"
            if int(p.get("alarmLevel") or 0) >= 2:
                status = "trouble"
            out.append({
                "name": name,
                "dn": f"neteco-{sn}",
                "source": "neteco",
                "status": status,
                "metered": False,
                "nominal_kw": round(_num(p.get("ratePowers")), 1),
                "now_kw": round(_num(p.get("currentPower")), 2),
                "today_kwh": round(_num(p.get("dayPower")), 1),
                "total_kwh": round(_num(p.get("totalPower")), 0),
                "total_rev": round(_num(p.get("income")), 0),
                "device_num": int(p.get("deviceNum") or 0),
                "address": ", ".join(x for x in [_unescape(p.get("city")), _unescape(p.get("country"))]
                                     if x and x != "-"),
                "lat": None, "lon": None, "connected": "",
            })
        return out


def get_neteco_stations(username: str, password: str) -> list:
    return NetEcoClient(username, password).login().get_plants()

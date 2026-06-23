"""
Collect the full rich dataset for every station (both regions) and write
output/data.json. Used by build_dashboard.py; can also be run standalone.
"""
import os
import json
import time
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from fusionsolar import FusionSolarClient

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# Manual override of installed capacity (kW) per station name. FusionSolar
# reports 0 for a site while it's offline (and the installer never set a fixed
# installedCapacity), so put known values here. Anything not listed falls back
# to FusionSolar's reported inverter capacity.
CAPACITY_KW = {
    # "Brenvei_K.K_navis": 150,   # offline — set its real installed kW here
}

TZ = timezone(timedelta(hours=3))   # Ukraine (EEST)
def _ms(dt): return int(dt.timestamp() * 1000)
def _num(v, d=0.0):
    try:
        n = float(v); return d if n <= -99999999 else n
    except (TypeError, ValueError):
        return d


def _safe(fn, *a, default=None):
    try:
        return fn(*a)
    except Exception as e:
        print(f"      ! {fn.__name__} failed: {type(e).__name__}: {str(e)[:60]}")
        return [] if default is None else default


def collect():
    client = FusionSolarClient(os.environ["FUSIONSOLAR_USER"],
                               os.environ["FUSIONSOLAR_PASSWORD"])
    now = datetime.now(TZ)
    t_now = _ms(now)
    t_daily = _ms(now - timedelta(days=95))
    t_monthly = _ms(now - timedelta(days=820))
    t_yearly = _ms(now - timedelta(days=2200))

    stations = []
    for region in client.iter_regions():
        for st in region.get_stations():
            dn = st.get("dn")
            name = st.get("name")
            print(f"   • {name} ({dn})")
            rec = {
                "name": name, "dn": dn, "region": region.region_code,
                "status": st.get("plantStatus") or "unknown",
                "nominal_kw": CAPACITY_KW.get(name) or _num(st.get("onlyInverterPower")),
                "now_kw": _num(st.get("currentPower")),
                "today_kwh": _num(st.get("dailyEnergy")),
                "month_kwh": _num(st.get("monthEnergy")),
                "year_kwh": _num(st.get("yearEnergy")),
                "total_kwh": _num(st.get("cumulativeEnergy")),
                "address": st.get("plantAddress") or "",
                "lat": st.get("latitude"), "lon": st.get("longitude"),
                "connected": (st.get("gridConnectedTime") or "")[:10],
            }
            kpi = _safe(region.get_station_kpi, dn, default={})
            rec["today_rev"] = _num(kpi.get("dailyIncome")) if isinstance(kpi, dict) else 0.0
            # keep only true inverters (exclude meters / power sensors, whose
            # 30014 signal is grid power and would corrupt the generation curve)
            devices = _safe(region.get_inverters, dn)
            rec["inverters"] = [d for d in devices
                                if "inverter" in ((d.get("type") or "") + " " + (d.get("name") or "")).lower()]
            inv_dns = [i["dn"] for i in rec["inverters"] if i.get("dn")]
            rec["today_curve"] = _safe(region.get_power_curve, inv_dns); time.sleep(0.4)
            rec["daily"] = _safe(region.get_history, dn, 4, t_daily, t_now); time.sleep(0.5)
            rec["monthly"] = _safe(region.get_history, dn, 5, t_monthly, t_now); time.sleep(0.5)
            rec["yearly"] = _safe(region.get_history, dn, 6, t_yearly, t_now); time.sleep(0.5)
            rec["alarms"] = _safe(region.get_alarms, dn)
            print(f"     curve={len(rec['today_curve'])} daily={len(rec['daily'])} "
                  f"monthly={len(rec['monthly'])} yearly={len(rec['yearly'])} "
                  f"inv={len(rec['inverters'])} alarms={len(rec['alarms'])}")
            stations.append(rec)
            time.sleep(0.6)

    bundle = {"updated": now.strftime("%Y-%m-%d %H:%M"), "tz": "Europe/Kyiv",
              "stations": stations}
    os.makedirs("output", exist_ok=True)
    with open("output/data.json", "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, ensure_ascii=False)
    return bundle


if __name__ == "__main__":
    b = collect()
    print(f"\nCollected {len(b['stations'])} stations -> output/data.json "
          f"({os.path.getsize('output/data.json')} bytes)")

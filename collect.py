"""
Collect the full energy dataset for every station (both regions) and write
output/data.json: live snapshot, today's energy balance, PV power curve, and
daily/monthly/yearly history of PV / load / grid import & export / self-use /
revenue (metered sites) — plus tariff and alarms.
"""
import os
import json
import time
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from fusionsolar import FusionSolarClient

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# Manual override of installed capacity (kW) per station name (FusionSolar
# reports 0 while a site is offline). Anything not listed uses the reported value.
CAPACITY_KW = {
    # "Brenvei_K.K_navis": 150,
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
    t_daily = _ms(now - timedelta(days=400))
    t_monthly = _ms(now - timedelta(days=1300))
    t_yearly = _ms(now - timedelta(days=4000))

    stations = []
    for region in client.iter_regions():
        for st in region.get_stations():
            dn, name = st.get("dn"), st.get("name")
            print(f"   • {name} ({dn})")
            rec = {
                "name": name, "dn": dn, "region": region.region_code,
                "status": st.get("plantStatus") or "unknown",
                "nominal_kw": CAPACITY_KW.get(name) or _num(st.get("onlyInverterPower")),
                "now_kw": _num(st.get("currentPower")),
                "address": st.get("plantAddress") or "",
                "lat": st.get("latitude"), "lon": st.get("longitude"),
                "connected": (st.get("gridConnectedTime") or "")[:10],
            }
            rec["price"] = _safe(region.get_station_price, dn, default={}); time.sleep(0.3)
            today = _safe(region.get_energy_today, dn, default={}); time.sleep(0.4)
            rec["metered"] = bool(today.get("metered"))
            rec["today"] = today

            # today's PV power curve: sum inverter active-power signals
            devices = _safe(region.get_inverters, dn)
            inverters = [d for d in devices
                         if "inverter" in ((d.get("type") or "") + " " + (d.get("name") or "")).lower()]
            agg = {}
            for inv in inverters:
                for p in _safe(region.get_inverter_curve, inv.get("dn")):
                    a = agg.setdefault(p["t"], {"sum": 0.0, "has": False})
                    if p["kw"] is not None:
                        a["sum"] += p["kw"]; a["has"] = True
                time.sleep(0.3)
            rec["n_inverters"] = len(inverters)
            rec["today_curve"] = [{"t": t, "kw": round(agg[t]["sum"], 2) if agg[t]["has"] else None}
                                  for t in sorted(agg)]

            rec["daily"] = _safe(region.get_history, dn, 4, t_daily, t_now, default=[]); time.sleep(0.5)
            rec["monthly"] = _safe(region.get_history, dn, 5, t_monthly, t_now, default=[]); time.sleep(0.5)
            rec["yearly"] = _safe(region.get_history, dn, 6, t_yearly, t_now, default=[]); time.sleep(0.5)
            rec["alarms"] = _safe(region.get_alarms, dn)
            print(f"     metered={rec['metered']} daily={len(rec['daily'])} monthly={len(rec['monthly'])} "
                  f"yearly={len(rec['yearly'])} curvePts={len(rec['today_curve'])} alarms={len(rec['alarms'])}")
            stations.append(rec)
            time.sleep(0.5)

    # --- Photomate / NetEco plants (separate platform: Кролевець, Жорнава) ---
    ne_user, ne_pw = os.environ.get("NETECO_USER"), os.environ.get("NETECO_PASSWORD")
    if ne_user and ne_pw:
        try:
            from neteco import NetEcoClient
            ne = NetEcoClient(ne_user, ne_pw).login()
            for p in ne.get_plants():
                try:
                    node = p["dn"].replace("neteco-", "")
                    hist = _safe(ne.get_plant_history, node, default={})
                    p.update({
                        "today": {"metered": False, "pv": p["today_kwh"]},
                        "price": {}, "n_inverters": p.get("device_num", 0), "alarms": [],
                        "daily": hist.get("daily", []), "monthly": hist.get("monthly", []),
                        "yearly": hist.get("yearly", []), "today_curve": hist.get("today_curve", []),
                    })
                    # estimate income per period from the lifetime effective €/kWh
                    eur_kwh = (p["total_rev"] / p["total_kwh"]) if p.get("total_kwh") else 0
                    for arr_ in (p["daily"], p["monthly"], p["yearly"]):
                        for d in arr_:
                            d["rev"] = round(d["pv"] * eur_kwh, 2)
                    p["today_rev"] = round(p["today_kwh"] * eur_kwh, 2)
                    p["eur_kwh"] = round(eur_kwh, 4)
                    stations.append(p)   # always keep the plant (history is best-effort)
                    print(f"   • {p['name']} (NetEco) now={p['now_kw']}kW daily={len(p['daily'])} "
                          f"monthly={len(p['monthly'])} yearly={len(p['yearly'])} curve={len(p['today_curve'])}")
                    time.sleep(0.4)
                except Exception as e:
                    print(f"   ! NetEco plant {p.get('name')} failed: {type(e).__name__}: {str(e)[:60]}")
        except Exception as e:
            print(f"   ! NetEco collection failed: {type(e).__name__}: {str(e)[:90]}")

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

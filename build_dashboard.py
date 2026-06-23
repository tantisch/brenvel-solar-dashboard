"""
Generate a self-contained HTML dashboard of ALL stations (both regions merged,
no region split) from live FusionSolar data.

Outputs:
    output/dashboard.html   - standalone page, host it anywhere
    output/data.json        - raw snapshot (for re-use / history)

Usage:
    ./venv/bin/python build_dashboard.py
"""
import os
import json
from datetime import datetime

from dotenv import load_dotenv

from fusionsolar import FusionSolarClient

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

STATUS = {
    "connected":    ("#16a34a", "Online"),
    "disconnected": ("#9ca3af", "Offline"),
    "trouble":      ("#f59e0b", "Warning"),
    "default":      ("#9ca3af", "Unknown"),
}


def num(value, default=0.0):
    try:
        n = float(value)
        return default if n <= -99999999 else n
    except (TypeError, ValueError):
        return default


def fmt(n, dp=0):
    return f"{n:,.{dp}f}"


def collect():
    client = FusionSolarClient(os.environ["FUSIONSOLAR_USER"],
                               os.environ["FUSIONSOLAR_PASSWORD"])
    rows = []
    for st in client.get_all_stations():
        rows.append({
            "name": st.get("name", "?"),
            "status": (st.get("plantStatus") or "default"),
            "nominal_kw": num(st.get("onlyInverterPower")),
            "now_kw": num(st.get("currentPower")),
            "today_kwh": num(st.get("dailyEnergy")),
            "month_kwh": num(st.get("monthEnergy")),
            "year_kwh": num(st.get("yearEnergy")),
            "total_kwh": num(st.get("cumulativeEnergy")),
            "address": st.get("plantAddress", ""),
            "connected_since": (st.get("gridConnectedTime") or "")[:10],
            "id": st.get("dn", ""),
        })
    # sort: problems first, then by current output desc
    rows.sort(key=lambda r: (r["status"] == "connected", -r["now_kw"]))
    return rows


CSS = """
<style>
  .sf-wrap{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
    color:#0f172a;background:#f1f5f9;margin:0;padding:24px;box-sizing:border-box}
  .sf-wrap *{box-sizing:border-box}
  .sf-head{display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:12px;margin-bottom:20px}
  .sf-title{font-size:26px;font-weight:800;letter-spacing:-.5px;margin:0;display:flex;align-items:center;gap:10px}
  .sf-sub{color:#64748b;font-size:13px;margin-top:4px}
  .sf-live{display:inline-flex;align-items:center;gap:6px;background:#dcfce7;color:#15803d;
    font-weight:600;font-size:12px;padding:5px 10px;border-radius:999px}
  .sf-dot{width:8px;height:8px;border-radius:50%;background:#22c55e;animation:sfp 1.6s infinite}
  @keyframes sfp{0%,100%{opacity:1}50%{opacity:.35}}
  .sf-kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:18px}
  .sf-kpi{background:#fff;border-radius:14px;padding:16px 18px;box-shadow:0 1px 3px rgba(15,23,42,.07)}
  .sf-kpi .k{font-size:12px;color:#64748b;font-weight:600;text-transform:uppercase;letter-spacing:.5px}
  .sf-kpi .v{font-size:24px;font-weight:800;margin-top:6px;letter-spacing:-.5px}
  .sf-kpi .u{font-size:13px;color:#94a3b8;font-weight:600}
  .sf-alert{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:12px;
    padding:12px 16px;font-size:14px;margin-bottom:18px;font-weight:500}
  .sf-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px;margin-bottom:22px}
  .sf-card{background:#fff;border-radius:16px;padding:18px;box-shadow:0 1px 3px rgba(15,23,42,.07);
    border-top:4px solid #e2e8f0;position:relative}
  .sf-card .top{display:flex;justify-content:space-between;align-items:center;gap:8px}
  .sf-name{font-weight:700;font-size:16px}
  .sf-badge{font-size:11px;font-weight:700;padding:3px 9px;border-radius:999px;color:#fff;white-space:nowrap}
  .sf-now{font-size:32px;font-weight:800;margin:12px 0 2px;letter-spacing:-1px}
  .sf-now span{font-size:15px;color:#94a3b8;font-weight:600}
  .sf-util{height:7px;background:#eef2f6;border-radius:6px;overflow:hidden;margin:8px 0 4px}
  .sf-util>div{height:100%;border-radius:6px}
  .sf-utiltxt{font-size:11px;color:#94a3b8;font-weight:600}
  .sf-mtable{display:grid;grid-template-columns:1fr 1fr;gap:8px 14px;margin-top:14px;font-size:13px}
  .sf-mtable .lbl{color:#94a3b8;font-weight:600;font-size:11px;text-transform:uppercase}
  .sf-mtable .val{font-weight:700;font-size:15px;margin-top:1px}
  .sf-addr{margin-top:14px;font-size:12px;color:#94a3b8;border-top:1px solid #f1f5f9;padding-top:10px}
  .sf-chart{background:#fff;border-radius:16px;padding:20px;box-shadow:0 1px 3px rgba(15,23,42,.07)}
  .sf-chart h3{margin:0 0 16px;font-size:15px;font-weight:700}
  .sf-bar{display:grid;grid-template-columns:140px 1fr 90px;align-items:center;gap:12px;margin-bottom:10px}
  .sf-bar .nm{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .sf-track{height:22px;background:#eef2f6;border-radius:6px;overflow:hidden}
  .sf-fill{height:100%;background:linear-gradient(90deg,#22c55e,#16a34a);border-radius:6px}
  .sf-bar .amt{font-size:13px;font-weight:700;text-align:right}
  .sf-foot{text-align:center;color:#94a3b8;font-size:12px;margin-top:22px}
  @media(max-width:760px){.sf-kpis{grid-template-columns:repeat(2,1fr)}.sf-bar{grid-template-columns:100px 1fr 70px}}
</style>
"""


def card(r):
    color, label = STATUS.get(r["status"], STATUS["default"])
    util = (r["now_kw"] / r["nominal_kw"] * 100) if r["nominal_kw"] > 0 else 0
    util = min(util, 100)
    return f"""
    <div class="sf-card" style="border-top-color:{color}">
      <div class="top">
        <div class="sf-name">{r['name']}</div>
        <div class="sf-badge" style="background:{color}">{label}</div>
      </div>
      <div class="sf-now">{fmt(r['now_kw'],1)}<span> kW now</span></div>
      <div class="sf-util"><div style="width:{util:.0f}%;background:{color}"></div></div>
      <div class="sf-utiltxt">{util:.0f}% of {fmt(r['nominal_kw'],0)} kW capacity</div>
      <div class="sf-mtable">
        <div><div class="lbl">Today</div><div class="val">{fmt(r['today_kwh'],1)} <small>kWh</small></div></div>
        <div><div class="lbl">This month</div><div class="val">{fmt(r['month_kwh'])} <small>kWh</small></div></div>
        <div><div class="lbl">This year</div><div class="val">{fmt(r['year_kwh'])} <small>kWh</small></div></div>
        <div><div class="lbl">Lifetime</div><div class="val">{fmt(r['total_kwh'])} <small>kWh</small></div></div>
      </div>
      <div class="sf-addr">📍 {r['address'] or '—'}</div>
    </div>"""


def bar(r, mx):
    pct = (r["today_kwh"] / mx * 100) if mx > 0 else 0
    return f"""
    <div class="sf-bar">
      <div class="nm">{r['name']}</div>
      <div class="sf-track"><div class="sf-fill" style="width:{pct:.0f}%"></div></div>
      <div class="amt">{fmt(r['today_kwh'],1)} kWh</div>
    </div>"""


def render_inner(rows, updated):
    online = sum(1 for r in rows if r["status"] == "connected")
    t_now = sum(r["now_kw"] for r in rows)
    t_today = sum(r["today_kwh"] for r in rows)
    t_month = sum(r["month_kwh"] for r in rows)
    t_year = sum(r["year_kwh"] for r in rows)
    t_life = sum(r["total_kwh"] for r in rows)
    problems = [r for r in rows if r["status"] != "connected"]

    kpis = [
        ("Live Power", fmt(t_now, 1), "kW"),
        ("Today", fmt(t_today), "kWh"),
        ("This Month", fmt(t_month), "kWh"),
        ("This Year", fmt(t_year), "kWh"),
        ("Lifetime", fmt(t_life / 1000, 1), "MWh"),
    ]
    kpi_html = "".join(
        f'<div class="sf-kpi"><div class="k">{k}</div>'
        f'<div class="v">{v} <span class="u">{u}</span></div></div>'
        for k, v, u in kpis)

    alert = ""
    if problems:
        names = ", ".join(f"{p['name']} ({STATUS.get(p['status'], STATUS['default'])[1].lower()})"
                          for p in problems)
        alert = f'<div class="sf-alert">⚠️ {len(problems)} station(s) need attention: {names}</div>'

    mx = max((r["today_kwh"] for r in rows), default=0)
    bars = "".join(bar(r, mx) for r in sorted(rows, key=lambda r: -r["today_kwh"]))
    cards = "".join(card(r) for r in rows)

    return CSS + f"""
    <div class="sf-wrap">
      <div class="sf-head">
        <div>
          <h1 class="sf-title">☀️ Brenvel Solar Fleet</h1>
          <div class="sf-sub">{len(rows)} locations · {online} online · updated {updated}</div>
        </div>
        <div class="sf-live"><span class="sf-dot"></span> LIVE</div>
      </div>
      <div class="sf-kpis">{kpi_html}</div>
      {alert}
      <div class="sf-grid">{cards}</div>
      <div class="sf-chart">
        <h3>Energy produced today by location</h3>
        {bars}
      </div>
      <div class="sf-foot">Auto-generated from FusionSolar · {updated}</div>
    </div>"""


def main():
    print("Pulling live data from all 5 stations...")
    rows = collect()
    updated = datetime.now().strftime("%Y-%m-%d %H:%M")

    os.makedirs("output", exist_ok=True)
    with open("output/data.json", "w", encoding="utf-8") as fh:
        json.dump({"updated": updated, "stations": rows}, fh, ensure_ascii=False, indent=2)

    inner = render_inner(rows, updated)
    html = ("<!DOCTYPE html>\n<html lang='en'>\n<head>\n<meta charset='utf-8'>\n"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>\n"
            "<title>Brenvel Solar Fleet</title>\n</head>\n<body style='margin:0'>\n"
            + inner + "\n</body>\n</html>")
    with open("output/dashboard.html", "w", encoding="utf-8") as fh:
        fh.write(html)

    # inner-only fragment (style + markup, no document tags) for embedding/preview
    with open("output/dashboard_inner.html", "w", encoding="utf-8") as fh:
        fh.write(inner)

    print(f"Done. {len(rows)} stations. Dashboard -> output/dashboard.html")


if __name__ == "__main__":
    main()

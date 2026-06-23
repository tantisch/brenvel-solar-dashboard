"""
Export a snapshot of every FusionSolar station (both regions) to the console
and a timestamped CSV file in ./output/.

Usage:
    ./venv/bin/python export_stations.py
"""
import os
import csv
from datetime import datetime

from dotenv import load_dotenv

from fusionsolar import FusionSolarClient

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))


def f(value, default=0.0):
    """Parse a FusionSolar numeric string; treat sentinels as missing."""
    try:
        n = float(value)
        return default if n <= -99999999 else n
    except (TypeError, ValueError):
        return default


# Column key -> (header, transform)
COLUMNS = [
    ("_region",          "Region",          str),
    ("name",             "Station",         str),
    ("plantStatus",      "Status",          str),
    ("onlyInverterPower","Nominal kW",      lambda v: f(v)),
    ("currentPower",     "Now kW",          lambda v: f(v)),
    ("dailyEnergy",      "Today kWh",       lambda v: f(v)),
    ("monthEnergy",      "Month kWh",       lambda v: f(v)),
    ("yearEnergy",       "Year kWh",        lambda v: f(v)),
    ("cumulativeEnergy", "Total kWh",       lambda v: f(v)),
    ("gridConnectedTime","Connected",       str),
    ("plantAddress",     "Address",         str),
    ("dn",               "Station ID",      str),
]


def main():
    user = os.environ["FUSIONSOLAR_USER"]
    pw = os.environ["FUSIONSOLAR_PASSWORD"]

    print("Logging in and reading all stations across both regions...\n")
    client = FusionSolarClient(user, pw)
    stations = client.get_all_stations()

    rows = []
    for st in stations:
        row = {}
        for key, header, fn in COLUMNS:
            row[header] = fn(st.get(key))
        rows.append(row)

    # --- console table (key numeric columns only) ---
    show = ["Region", "Station", "Status", "Nominal kW", "Now kW",
            "Today kWh", "Month kWh", "Year kWh", "Total kWh"]
    widths = {c: max(len(c), *(len(f"{r[c]:.1f}" if isinstance(r[c], float) else r[c])
                               for r in rows)) for c in show}
    line = "  ".join(c.ljust(widths[c]) for c in show)
    print(line)
    print("-" * len(line))
    for r in rows:
        cells = []
        for c in show:
            v = r[c]
            cells.append((f"{v:,.1f}" if isinstance(v, float) else str(v)).ljust(widths[c]))
        print("  ".join(cells))

    # totals
    tot_now = sum(r["Now kW"] for r in rows)
    tot_today = sum(r["Today kWh"] for r in rows)
    tot_month = sum(r["Month kWh"] for r in rows)
    tot_year = sum(r["Year kWh"] for r in rows)
    tot_all = sum(r["Total kWh"] for r in rows)
    print("-" * len(line))
    print(f"TOTAL ({len(rows)} stations):  "
          f"Now {tot_now:,.1f} kW | Today {tot_today:,.1f} | Month {tot_month:,.1f} | "
          f"Year {tot_year:,.1f} | Lifetime {tot_all:,.0f} kWh")

    # --- CSV ---
    os.makedirs("output", exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    path = os.path.join("output", f"stations_{stamp}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow([h for _, h, _ in COLUMNS] + ["Snapshot time"])
        snap = datetime.now().strftime("%Y-%m-%d %H:%M")
        for st in stations:
            writer.writerow([fn(st.get(k)) for k, _, fn in COLUMNS] + [snap])
    print(f"\nSaved: {path}")


if __name__ == "__main__":
    main()

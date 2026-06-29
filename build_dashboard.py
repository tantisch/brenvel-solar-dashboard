"""
Build the self-contained Brenvel Solar Fleet dashboard:
collect live data from all stations (both regions), inject it into the
site template, and write output/dashboard.html (+ output/data.json).

Usage:
    ./venv/bin/python build_dashboard.py
"""
import os
import json

from collect import collect

HERE = os.path.dirname(__file__)


def load_prices():
    """Shared, persistent per-station tariffs committed to the repo
    (config/prices.json). Edited via the dashboard -> GitHub issue ->
    price-update workflow, so a change made by anyone sticks for everyone."""
    p = os.path.join(HERE, "config", "prices.json")
    try:
        d = json.load(open(p, encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except Exception as e:
        print(f"  prices.json not loaded ({e}); using defaults")
    return {"uah_per_eur": 42, "stations": {}}


def apply_prices(bundle, prices):
    """Attach a per-station tariff object `pr` to each station and the global
    FX rate. `pr.def_eur` is the effective lifetime price (rev/pv) used as the
    default; `pr.eur`/`pr.uah` are explicit per-MWh overrides (None if unset)."""
    fx = float(prices.get("uah_per_eur", 42) or 42)
    overrides = prices.get("stations", {}) or {}

    # default effective EUR/MWh per station from lifetime revenue / generation
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

    for s in bundle["stations"]:
        name = s["name"]
        ov = overrides.get(name) or {}
        eur = ov.get("eur_mwh")
        uah = ov.get("uah_mwh")
        eur = float(eur) if isinstance(eur, (int, float)) and eur > 0 else None
        uah = float(uah) if isinstance(uah, (int, float)) and uah > 0 else None
        s["pr"] = {
            "custom": bool(eur or uah),
            "eur": eur,
            "uah": uah,
            "def_eur": defs.get(name) or fleet_def,
        }

    bundle["fx"] = fx
    bundle["repo"] = os.environ.get("REPO_SLUG", "tantisch/brenvel-solar-dashboard")


def main():
    print("Collecting live data from all stations...")
    bundle = collect()   # also writes output/data.json
    apply_prices(bundle, load_prices())

    template = open(os.path.join(HERE, "site_template.html"), encoding="utf-8").read()
    payload = json.dumps(bundle, ensure_ascii=False).replace("</", "<\\/")  # script-safe
    html = template.replace("__DATA__", payload)

    os.makedirs(os.path.join(HERE, "output"), exist_ok=True)
    out = os.path.join(HERE, "output", "dashboard.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)

    print(f"Wrote {out} ({len(html):,} bytes) for {len(bundle['stations'])} stations.")


if __name__ == "__main__":
    main()

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


def main():
    print("Collecting live data from all stations...")
    bundle = collect()   # also writes output/data.json

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

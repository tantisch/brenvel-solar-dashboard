# Brenvel Solar Fleet — dashboard & parser

Pulls live data for all **5 solar stations** (across both FusionSolar regions) and
publishes a single password-protected dashboard that anyone with the link + password
can open — auto-refreshed every 30 minutes by GitHub Actions.

All 5 locations appear in one unified view (no region split).

## What's here

| File | Purpose |
|------|---------|
| `fusionsolar.py` | Multi-region FusionSolar web client: two-region login + data (live, intraday curve, daily/monthly/yearly history, inverters, alarms). |
| `collect.py` | Pulls the full dataset for every station → `output/data.json`. |
| `site_template.html` | The dashboard UI (overview + per-location deep-dive, Chart.js). Data is injected at build time. |
| `build_dashboard.py` | Runs `collect.py` and injects the data into the template → `output/dashboard.html` (self-contained). |
| `export_stations.py` | A flat CSV snapshot of all stations in `output/`. |
| `.github/workflows/dashboard.yml` | Cloud job (schedule + on push) that builds, encrypts, and publishes the dashboard. |
| `.env` | Your FusionSolar login (gitignored — never committed). |

## Run it locally

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python build_dashboard.py    # → open output/dashboard.html
./venv/bin/python export_stations.py     # → CSV in output/
```

Credentials are read from `.env` (this file is gitignored — never committed):

```
FUSIONSOLAR_USER=your-fusionsolar-login@example.com
FUSIONSOLAR_PASSWORD=your-password
```

## Publish the auto-updating dashboard (GitHub Pages)

The published page is **AES-encrypted** (StatiCrypt) and needs a password to view, so
it's safe to use a **public** repo — which keeps GitHub Actions free and unlimited.
Your FusionSolar login is **never** in the repo: it lives only in encrypted GitHub
Secrets, and `.env` is gitignored.

1. **Create a new GitHub repo** (public is fine — there are no secrets in the code) and
   push this project to it.
2. **Add 3 repository secrets** under *Settings → Secrets and variables → Actions → New
   repository secret*:
   - `FUSIONSOLAR_USER` — your FusionSolar login email
   - `FUSIONSOLAR_PASSWORD` — your FusionSolar password
   - `DASHBOARD_PASSWORD` — the password your team will type to view the dashboard
3. **Enable Pages**: *Settings → Pages → Build and deployment → Source = GitHub Actions*.
4. **Run it once**: *Actions → "Build & publish solar dashboard" → Run workflow*. After it
   finishes, the dashboard URL is shown in the deploy step (and under *Settings → Pages*).
   It looks like `https://<your-user>.github.io/<repo>/`.
5. Done — it now rebuilds every 30 minutes during daylight. Share the URL + the
   `DASHBOARD_PASSWORD` with your team.

### Notes
- **Change the schedule** in `.github/workflows/dashboard.yml` (the `cron` line). Times are
  UTC; Ukraine is UTC+2 (winter) / UTC+3 (summer).
- **Change the password** any time by updating the `DASHBOARD_PASSWORD` secret — it applies
  on the next run.
- GitHub disables scheduled workflows after **60 days with no commits** to the repo. If
  updates stop, push any commit (or click *Run workflow*) to wake it back up.

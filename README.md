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
| `notify.py` | Telegram bot brain: fault alerts (day-only) + daily/weekly/monthly reports from `output/data.json`. |
| `telegram.py` | Tiny stdlib Telegram Bot API client (`send`, plus `chatid`/`test` setup helpers). |
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

## Telegram bot (alerts + reports)

The same cloud loop also runs a Telegram bot (`notify.py`). It sends:

- **Fault alerts** — when a station drops to *disconnected / warning* **during the day**
  (while the rest of the fleet is generating, or 09:00–16:00), and a **recovery** note when
  it returns. Nightly inverter sleep never alerts, so there is no night/morning spam.
- **Reports** — a daily digest (generation + revenue per station + fleet total) at
  **21:00** Kyiv, a weekly one on Mondays, and a monthly one on the 1st.

The bot is **skipped unless** the `TELEGRAM_BOT_TOKEN` secret is set, so it is fully
optional. **Recipients are automatic** — anyone who presses **Start** on the bot (or any
group it is added to) is subscribed and gets every alert and report; chats that block or
remove the bot are pruned automatically. Bot state (station health, the subscriber chat-id
list, and last report dates — never generation or revenue numbers) is kept on an orphan
`bot-state` branch so it survives across runs.

**Setup:**

1. **Create the bot** — in Telegram open **@BotFather** → `/newbot` → pick a name. It gives
   you a token like `123456789:AA...`.
2. **Add 1 repository secret** (*Settings → Secrets and variables → Actions*):
   `TELEGRAM_BOT_TOKEN`. *(Optional: `TELEGRAM_CHAT_ID` to always include one specific chat
   even before anyone subscribes; a repo **variable** `REPORT_HOUR`, 0–23, changes the
   daily/weekly/monthly send hour — default 21.)*
3. **Restart the loop** — *Actions → "Build & publish solar dashboard" → Run workflow*.
4. **Subscribe** — in Telegram press **Start** on the bot (and/or add it to a team group and
   send a message there). Within one cycle (~15 min) it welcomes each new subscriber, and
   alerts/reports follow. Everyone who starts the bot is added automatically.

Test the message wording without sending anything:

```bash
TELEGRAM_DRY_RUN=1 python3 notify.py /tmp/bot_state.json     # prints alerts/reports
python3 telegram.py chatid    # optional: paste token, see who has messaged the bot
```

### Notes
- **Change the schedule** in `.github/workflows/dashboard.yml` (the `cron` line). Times are
  UTC; Ukraine is UTC+2 (winter) / UTC+3 (summer).
- **Change the password** any time by updating the `DASHBOARD_PASSWORD` secret — it applies
  on the next run.
- GitHub disables scheduled workflows after **60 days with no commits** to the repo. If
  updates stop, push any commit (or click *Run workflow*) to wake it back up.

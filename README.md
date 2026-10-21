# Plug Employ Intelligence

A lead-generation web app for franchise owners. It finds local businesses, scores them for
advertising fit, and tracks them in each customer's own lead tracker.
See [docs/PLAN.md](docs/PLAN.md) for the full plan and decisions.

**Current stage:** Phase 0. A hello-world page that confirms the database connection.

## What you need on your Mac

- **Python 3.12** from python.org
- **Postgres.app** (PostgreSQL 17). Its elephant icon in the menu bar should show "Running".
- **Git**, which comes with Apple's developer tools

## Start the app

1. Make sure Postgres.app is running (elephant in the menu bar).
2. Open the **Terminal** app.
3. Paste this and press Return:

   ```bash
   ~/Documents/plug-employ-intelligence/start.sh
   ```

4. Open **http://127.0.0.1:8000** in your browser.

To **stop** the app, click the Terminal window and press **Control + C**.

## Project layout

| Path | What it is |
|---|---|
| `app/main.py` | The web app's pages |
| `app/db.py` | Database connection |
| `app/config.py` | Reads settings from `.env` |
| `app/templates/` | Page designs (Jinja + HTMX + Tailwind) |
| `docs/PLAN.md` | Plan and decisions |
| `.env` | **Your private settings and keys. Never committed.** |
| `.env.example` | Blank template showing which settings exist |
| `requirements.txt` | Python add-ons the app needs |
| `.venv/` | This project's private Python box (not committed) |

## Setting up on a new computer

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then edit .env with real values
/Applications/Postgres.app/Contents/Versions/latest/bin/createdb -h localhost plug_employ_dev
```

## Security rules

- API keys go **only** in `.env`. Never in code, never in Git, never in chat.
- Only official APIs and permitted website access (respect `robots.txt`).

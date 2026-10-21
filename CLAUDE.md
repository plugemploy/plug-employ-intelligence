# Plug Employ Intelligence: notes for Claude

Full plan and decisions: docs/PLAN.md. How to run: README.md.

## Working with Savannah
- Founder, no programming experience. Explain in plain, brief language.
- Ask permission before installing anything or running commands. Group steps into one approval.
- Never ask for API keys in chat. Keys go only in `.env` (never committed).
- Keep replies short. Don't re-explain things already in README/PLAN.

## Rules
- Mac: 2020 Intel MacBook Air, 8 GB memory, macOS Sonoma. No Docker.
- Stack: Python 3.12 (python.org), FastAPI, Jinja + HTMX + Tailwind, PostgreSQL 17 (Postgres.app),
  SQLAlchemy + Alembic, APScheduler. Add-ons live in `.venv`, listed in requirements.txt.
- Every table with customer data has `account_id`.
- Official APIs only. Respect robots.txt. No scraping workarounds.
- API budget: $50/month during the prototype.
- Test ZIPs: 37934, 37922, 37932, 37923, 37919, 37909.
- Lead tracker: status, notes, follow-up dates. No calling features. Close CRM is optional.

## Commands
- Start app: `./start.sh`, then open http://127.0.0.1:8000
- psql: /Applications/Postgres.app/Contents/Versions/latest/bin/psql -h localhost plug_employ_dev
- Install add-ons: `.venv/bin/pip install -r requirements.txt`

## Status
- Phase 0 done (2026-10-21): hello-world page with database check.
- Phase 1 done (2026-10-21): accounts (dropdown, no logins yet) and lead tracker. Alembic for DB changes. Sample data: `scripts/seed.py`. Tests: `.venv/bin/python -m pytest`.
- Phase 2 done (2026-10-21): Google Places (New) Text Search on /search, filters, dedupe, budget guard (api_usage table, $0.035/request at full price). Real leads go in account "Plug Employ (real leads)".
- Next: Phase 3, Claude scoring.

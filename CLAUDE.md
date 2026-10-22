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
- Live app (since 2026-10-22): https://plug-employ-leads.onrender.com (Render, Blueprint in render.yaml, deploys on push to main).
  GitHub: plugemploy/plug-employ-intelligence (private). The Mac copy is for testing changes only; nightly search is off there.
- Start app on the Mac (testing): `./start.sh`, then open http://127.0.0.1:8000
- psql: /Applications/Postgres.app/Contents/Versions/latest/bin/psql -h localhost plug_employ_dev
- Install add-ons: `.venv/bin/pip install -r requirements.txt`

## Status
- Phase 0 done (2026-10-21): hello-world page with database check.
- Phase 1 done (2026-10-21): accounts (dropdown, no logins yet) and lead tracker. Alembic for DB changes. Sample data: `scripts/seed.py`. Tests: `.venv/bin/python -m pytest`.
- Phase 2 done (2026-10-21): Google Places (New) Text Search on /search, filters, dedupe, budget guard (api_usage table, $0.035/request at full price). Real leads go in account "Plug Employ (real leads)".
- Phase 3 built (2026-10-21): Claude Haiku 5.5 scores leads 1-10 (app/scoring.py), Score button on leads list, auto-score after search. Not yet run for real.
- Nightly search built (2026-10-21): app/nightly.py, APScheduler checks every 15 min and catches up if the Mac slept. /nightly page. Real account on at 2am, 20 searches.
- Usage page + per-account limits + website check built (2026-10-21). See PLAN.md Business model.
- My business page (description, ZIPs, industries per account) built (2026-10-21). Gmail connect (gmail.readonly, token encrypted with APP_SECRET_KEY) replaces the contacts upload: Emailed tag + follow-up after 14 quiet days. Setup: docs/GMAIL_SETUP.md. Connect only via http://127.0.0.1:8000. Full re-score of real leads is saved for the final test run.
- Learning from results built (2026-10-21): Won/Not interested + reason feed past_results into scoring; What's working panel.
- #3 built (2026-10-21): Gmail drafts (app/outreach.py, never sends to leads), Monday 8am digest to self (app/digest.py), renewal follow-up 30 days before contract_end. Needs gmail.compose scope + reconnect.
- Close CRM built (2026-10-21): app/close_crm.py sends leads as Potential (dedupe by domain/name), syncs VA statuses back nightly (STATUS_MAP), optional nightly auto-send of 9+. CLOSE_API_KEY is one org for now; per-account later.
- Multi-company + Google sign-in built (2026-10-21): organizations > accounts > users (owner/admin/publisher), app/auth.py (encrypted session cookie), /signup, /admin team page. First sign-in on an empty users table claims the "Plug Employ" org. Close limited to that org (close_crm.OWNER_ORG).
- Setup wizard + industry picker built (2026-10-21): /setup (description, ZIPs, ~145 grouped business types in app/catalog.py with search, select all, custom; Claude suggests from description), /setup/first starter search (10 searches). New accounts redirect to /setup until done.
- Plans built (2026-10-21): app/plans.py Personal $99 ($15 allowance), Wholesale $60/publisher ($10), Owner (only .env cap). organizations.plan_tier. Searches-left meter, nightly pacing (nightly_quota), Find leads skips searches done in last 30 days.
- Stripe built (2026-10-21): app/billing.py, /billing (Checkout, return, Stripe portal), daily status sync; plans beta ($49, $8, 30-day trial), founding ($79), personal, wholesale (per publisher). Prices auto-created by lookup key pei_<tier>_monthly. Cancelled = searches paused.
- Owner/manager names (2026-10-21): website check also reads a linked About/Team page (robots.txt respected); scoring returns contact_name/contact_title only when the website states them; used in drafts, Close contact, digest.
- Delete/restore leads + duplicate check on Add lead + Gmail "we never sell your data" checkbox (2026-10-22).
- Beta prep Day 1 (2026-10-22): plans Standard $99 (no Gmail) / Pro $149 / Premium placeholder / free 30-day Beta for
  new sign-ups; per-plan nightly max; no shared overall cap; Send feedback box (owner reads /feedback); secure cookies on https.
- Premium $299 (2026-10-22): 20 ZIPs x 70 business types, shown as unlimited; limits enforced on save.
- Beta plan: Day 2 put it online (Render + domain), Day 3 test as a new customer and invite testers.
- Next: Stripe test keys from Savannah, cloud hosting + domain, results tracking for beta, per-customer Close, territory lock, Google verification.

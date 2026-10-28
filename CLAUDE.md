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
- Live app: https://app.plugemploy.com since 2026-10-25 (GoDaddy CNAME app -> plug-employ-leads.onrender.com; APP_BASE_URL set in Render; Google OAuth redirect URIs for both). Render (Blueprint in render.yaml, deploys on push to main).
  Render web service is 2 GB / 1 CPU (Standard) by 2026-10-28, OVERTURE_PARALLEL=2 in Render (set in Render's dashboard, not render.yaml): one
  Overture/DuckDB search peaks near 500 MB; overture.fetch_places runs OVERTURE_PARALLEL Overture reads at once (default 1; 2-3 needs 2 GB);
  a ZIP read is cached 12h and cached ZIPs skip the line. nightly.run_for_account locks per account only.
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
- Live on Render + background Find leads with progress bar + newest/date-added filters + owner-only costs (2026-10-22).
- Beta results (2026-10-22): statuses New > Contacted > Appointment set > Appointment shown > Sold (+ Not interested);
  each step's first date kept (contacted_at..sold_at). Sold leads: MRR, contract months (sets renewal), one-time Ad
  Management Fee. My results page (everyone); /beta + /beta.csv for the owner (companies with at least one user).
  Testers sign up at /signup and must be Google test users.
- Dashboard (2026-10-22): /dashboard donut of leads by status (app/dashboard.py, colors checked for color-blind
  readers); clicking a slice or row loads that status's leads below (/dashboard/breakdown).
- Sale pop-up + beta survey (2026-10-22): marking Sold pops up "Log this sale" (MRR, months, fee, found_elsewhere);
  Dashboard reminds about sales missing amounts. Beta orgs get a one-time survey pop-up from day 25 (would pay/plan,
  0-10 recommend, hours saved, quote + permission; "Ask me later" = 3 days); preview at /survey. All on /beta + CSV.
- Close per account (2026-10-22): each account pastes its own Close API key on My business (verified with Close
  /me, stored encrypted with APP_SECRET_KEY; close_crm.key_for). The .env CLOSE_API_KEY is only a fallback for the
  "Plug Employ" org. Close is on Beta/Pro/Premium (plans "close"), not Standard. Unknown Close statuses change nothing.
- Beta plan: Day 2 put it online (Render + domain), Day 3 test as a new customer and invite testers.
- Overture lead source (2026-10-22): Find leads + nightly search use Overture Maps' open places data (app/overture.py,
  DuckDB reads one ZIP's area from the public S3 release; storable/shareable under CDLA-Permissive/Apache/CC0), not
  Google Places (Google's terms only allow storing place IDs). ZIP areas from app/data/zip_centroids.csv (Census ZCTA
  2025). Business types -> Overture categories in app/overture_types.py. Skips closed, confidence < 0.5, no phone/website,
  duplicates (links older Google leads via overture_id); max 30 added per search, independents before brands.
  Plans count searches (plans "searches"), not dollars; allowance_usd now only caps Claude cost.
- Launch bundle (2026-10-23): Overture is the live lead source (Google Places no longer called; older Google leads
  keep their stored data pending attorney). Reasons to reach out (app/signals.py: nightly re-read of top 40 leads'
  sites + News/Events page, Claude lists timely items; LeadSignal; Dashboard card, lead box, 🔔 badge/filter).
  Like your customers card (dashboard.like_your_customers). Local pages to watch (app/local_pages.py: up to 5 URLs on
  My business, weekly, ribbon cuttings/new members -> signals or new leads). New in your territory
  (app/new_in_area.py: per-ZIP Overture id snapshots per release; first release is a baseline). Scoring: affluent
  homeowners, multi-location OK, founded_year from site, full 1-10 range; Re-score all my leads button.
- Client import (2026-10-25): /clients/import (My business link; owner can import for any account from /beta).
  CSV with flexible headings (app/client_import.py); rows marked Sold (existing lead matched via find_duplicate,
  else added) with MRR/months/fee/start date + renewal; Lead.source="import" is excluded from results metrics.
- One $49 plan (2026-10-28): billing.SELLABLE = ("pro",), label "Plug Employ", everything included, $6.50 Claude cap;
  Stripe price lookup key includes the price (pei_pro_49_monthly). Survey asks "Yes, at $49/month".
- Free search link (2026-10-28): /try signs up new companies on plan "free" (1 ZIP x 1 business type, one search ever,
  no Gmail/Close); existing users keep their plan. 15 seconds after its leads are ready (plans.first_leads_at) a $49 subscribe pop-up shows
  (main.subscribe_offer; "Maybe later" hides the pop-up a day). A purple Subscribe banner shows at once above every page (stays after Maybe later); banner load marks offer_seen_at. /signup still gives the 30-day Beta.
- Free search tracker + legal pages (2026-10-28): /beta shows /try sign-ups (signup_offer, offer_seen_at,
  checkout_started_at) apart from testers. /privacy and /terms are DRAFTS for the attorney; CONTACT_EMAIL env shows there.
- Live (2026-10-28): Stripe on the LIVE key (restricted rk_live_, "Full access except sensitive operations", account
  East TN Publication LLC); billing.sync unlinks subscriptions Stripe doesn't know. Google sign-in published (In production;
  Gmail scopes still unverified). CONTACT_EMAIL=admin@plugemploy.com. Email campaign link: /try.
- Nearby + setup counts (2026-10-28): one Overture read per ZIP (overture.fetch_area) keeps the ZIP plus reliable places
  within nearby_miles (2.5x ZIP radius, 3-10 mi, closest 25k). A search with < FILL_TO (15) good in-ZIP matches adds the
  closest nearby ones (Lead.nearby "Brighton, 6 mi from 48178", zip_code = their own ZIP; already-saved nearby ones don't
  count, so the next search reaches further). /setup/estimates shows "N · +M nearby" beside each type on /setup and
  My business (reads up to 5 ZIPs in the background via overture.warm). Tests block online reads (conftest).
- Next: attorney review of /privacy + /terms, Google verification (Gmail), territory lock, Google verification, per-account Close status mapping.

# Plug Employ Intelligence: Plan and Decisions

## What we're building

A lead-generation web app for franchise owners. It:

1. **Finds** local businesses by ZIP code and industry using the Google Places API.
2. **Scores** each business for advertising fit using the Claude API.
3. **Shows** the leads in each customer's own lead tracker: status, notes and follow-up dates.
   There are **no calling features**.
4. Optionally **connects** to Close CRM.

The prototype runs locally on a Mac and must later move to cloud hosting.

## Decisions already made

### Computer
- 2020 MacBook Air, Intel Core i5, 8 GB memory, macOS Sonoma 14.8.
- **No Docker yet.** It's too heavy for this Mac.
- Python 3.12 comes from the official python.org installer (3.12.10).
- PostgreSQL 17 comes from Postgres.app. Homebrew was skipped because its Intel support is shrinking.

### Technology stack
| Piece | Choice |
|---|---|
| Language | Python 3.12 |
| Web framework | FastAPI |
| Pages | Jinja templates + HTMX + Tailwind CSS |
| Database | PostgreSQL |
| Database code / changes | SQLAlchemy + Alembic |
| Scheduled jobs | APScheduler |

### Data rules
- **Every database table that holds customer data has an `account_id` column from day one**,
  so each customer only ever sees their own data.

### Security rules
- API keys go **only** in the `.env` file, which is never committed to Git.
- `.env.example` shows which settings exist, with no real values.
- Keys are never pasted into chat.

### Data-collection rules
- Only official APIs and permitted website access. Respect `robots.txt`.
- No scraping workarounds.

### Business model
- Sold to franchise owners as a **monthly subscription** (priced above $50/month).
- Each account has its own monthly API cost limit (`accounts.monthly_budget_usd`), set per plan,
  to protect margins. The `.env` `MONTHLY_API_BUDGET_USD` is an overall safety cap for the prototype.
- Each account sets its own business description, ZIP codes and industries on "My business".
- Gmail (n2co.com Workspace accounts): owners connect Gmail read-only; leads they've emailed get
  tagged and a follow-up is added after 14 quiet days. Only sender/recipient domain and date are used.
  Works for up to 100 test users now; selling publicly needs Google verification + paid security
  review, unless N2's Workspace admin registers it as an Internal app. N2's admin can also block it.
- Before selling: real logins, billing, and cloud hosting.

### Website reading
- Only each business's own homepage, only when its robots.txt allows, one request per site,
  with our own user agent. Text is used to sharpen Claude's score (chain vs. local, events).

### Budget
- **$50/month cap** for API usage during the prototype.

### First test area (Farragut and West Knoxville, TN)
ZIP codes: **37934, 37922, 37932, 37923, 37919, 37909**

## Phases

- **Phase 0 (current):** set up the computer and project, plus a hello-world page that confirms the
  database connection. No API accounts yet.
- **Later phases (to be planned):** accounts and lead tracker, Google Places search, Claude
  scoring, budget tracking, optional Close CRM connection, and the move to cloud hosting.

"""App settings, read from the .env file (never from code)."""

import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
# Render gives "postgresql://..." (or "postgres://"); this app talks to Postgres through psycopg 3.
for _prefix in ("postgres://", "postgresql://"):
    if DATABASE_URL.startswith(_prefix):
        DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL.removeprefix(_prefix)
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
MONTHLY_API_BUDGET_USD = float(os.getenv("MONTHLY_API_BUDGET_USD", "50"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "")
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
COOKIE_SECURE = APP_BASE_URL.startswith("https://")  # online: cookies only travel over https
CLOSE_API_KEY = os.getenv("CLOSE_API_KEY", "")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "")  # shown on the privacy policy and terms pages
# Overture reads that may run at once. Each peaks near 500 MB, so 1 on a 1 GB server; 2 or 3 on 2 GB.
OVERTURE_PARALLEL = max(1, int(os.getenv("OVERTURE_PARALLEL", "1")))

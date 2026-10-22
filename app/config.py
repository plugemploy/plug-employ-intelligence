"""App settings, read from the .env file (never from code)."""

import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
MONTHLY_API_BUDGET_USD = float(os.getenv("MONTHLY_API_BUDGET_USD", "50"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "")
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

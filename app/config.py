"""App settings, read from the .env file (never from code)."""

import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
MONTHLY_API_BUDGET_USD = float(os.getenv("MONTHLY_API_BUDGET_USD", "50"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

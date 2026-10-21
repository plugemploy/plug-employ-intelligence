"""Load fake sample accounts and leads for testing. Safe to run again: it replaces old samples.

Run: .venv/bin/python scripts/seed.py
"""

import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db import engine
from app.models import STATUSES, Account, FollowUp, Lead, LeadNote

ZIPS = ["37934", "37922", "37932", "37923", "37919", "37909"]
INDUSTRIES = ["Restaurant", "Dentist", "Auto repair", "Fitness", "Salon", "HVAC", "Real estate", "Landscaping"]
WORDS = ["Sample", "Example", "Testville", "Demo", "Placeholder", "Mockingbird", "Fictional", "Pretend"]
NOTES = ["Left a voicemail.", "Owner asked for pricing by email.", "Busy season, try again next month.",
         "Interested in a spring campaign.", "Met at chamber mixer."]


def run():
    random.seed(42)
    with Session(engine) as db:
        db.execute(delete(Account).where(Account.name.like("Sample:%")))
        for acct_name in ["Sample: Farragut Franchise", "Sample: West Knox Franchise"]:
            acct = Account(name=acct_name)
            db.add(acct)
            db.flush()
            for i in range(15):
                industry = random.choice(INDUSTRIES)
                lead = Lead(account_id=acct.id,
                            business_name=f"{random.choice(WORDS)} {industry} {i + 1} (FAKE)",
                            address=f"{100 + i * 7} Example Rd", zip_code=random.choice(ZIPS),
                            phone=f"555-01{i:02d}", website="https://example.com",
                            industry=industry, status=random.choice(STATUSES))
                db.add(lead)
                db.flush()
                for _ in range(random.randint(0, 2)):
                    db.add(LeadNote(account_id=acct.id, lead_id=lead.id, text=random.choice(NOTES)))
                if random.random() < 0.7:
                    db.add(FollowUp(account_id=acct.id, lead_id=lead.id,
                                    due_date=date.today() + timedelta(days=random.randint(-3, 14)),
                                    task="Call back"))
        db.commit()
    print("Loaded 2 sample accounts with 15 fake leads each.")


if __name__ == "__main__":
    run()

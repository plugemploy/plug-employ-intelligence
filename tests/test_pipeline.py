"""One pass per new lead: website read once, scored right away, reasons to reach out from the same visit."""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import pipeline, scoring, signals, website
from app.db import engine
from app.models import Account, ApiUsage, Lead, LeadSignal


def test_reads_scores_and_finds_reasons_in_one_pass(monkeypatch):
    import app.pipeline as p
    monkeypatch.setattr(p, "ANTHROPIC_API_KEY", "x")
    visits = []
    monkeypatch.setattr(website, "read_site", lambda url: visits.append(url) or ((
        "ok", "Family bakery since 1990.", "", "Family bakery. Grand opening of our second shop on Main St!",
        "https://bake.test/news") if url else ("no_site", "", "", "", "")))
    monkeypatch.setattr(scoring, "ask_claude", lambda lead, system: (
        scoring.LeadScore(score=9 if "bakery" in lead.website_text else 1, reason="Local bakery.", contact_name="",
                          contact_title=""), 0.0001))
    asked = []
    monkeypatch.setattr(signals, "ask_claude", lambda lead, text, today: asked.append(text) or (
        [signals.Reason(kind="opening", text="Opening a second shop on Main St.", date="")], 0.0001))
    with Session(engine) as db:
        a = Account(name="Test: pipeline", target_zips="37934", industries="Bakery", monthly_budget_usd=50)
        db.add(a)
        db.flush()
        db.add_all([Lead(account_id=a.id, business_name=f"Test Bakery {i}", website=f"bake{i}.test") for i in range(3)]
                   + [Lead(account_id=a.id, business_name="Test No Site")])
        db.commit()
        stats = pipeline.run(db, a.id, reasons=True)
        assert stats == {"read": 4, "scored": 4, "reasons": 3, "failed": 0}
        assert sorted(visits) == ["", "bake0.test", "bake1.test", "bake2.test"]  # each site visited once
        assert len(asked) == 3 and "Grand opening" in asked[0]  # reasons from the same visit's News page
        leads = db.scalars(select(Lead).where(Lead.account_id == a.id)).all()
        assert all(l.website_checked_at and l.score for l in leads)
        assert all(l.signals_checked_at for l in leads if l.website)
        assert db.scalars(select(LeadSignal).where(LeadSignal.account_id == a.id)).first().source_url == "https://bake.test/news"
        # nothing left to do: a second run reads nothing
        assert pipeline.run(db, a.id, reasons=True)["read"] == 0
        # without reasons (paid searches: reasons come from the nightly check), Claude isn't asked for them
        db.add(Lead(account_id=a.id, business_name="Test Bakery 9", website="bake9.test"))
        db.commit()
        asked.clear()
        assert pipeline.run(db, a.id)["scored"] == 1 and asked == []
        db.execute(delete(ApiUsage).where(ApiUsage.account_id == a.id))
        db.delete(a)
        db.commit()

"""Checks the website reader and per-account limits (no real network, no cost)."""

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import places, scoring, website
from app.db import engine
from app.models import Account, ApiUsage, Lead


@pytest.fixture
def account_id():
    with Session(engine) as db:
        a = Account(name="Test: Website", monthly_budget_usd=1)
        db.add(a)
        db.flush()
        db.add_all([Lead(account_id=a.id, business_name="Has Site", website="https://example.com", score=9),
                    Lead(account_id=a.id, business_name="No Site", score=6)])
        db.commit()
        aid = a.id
    yield aid
    with Session(engine) as db:
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.commit()


def test_html_to_text_drops_scripts():
    html = "<html><script>var x=1</script><h1>Family owned</h1> <p>Sponsor of the 5K</p></html>"
    assert website.html_to_text(html) == "Family owned Sponsor of the 5K"


def test_robots_blocks(monkeypatch):
    import httpx

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx.Response(200, text="<p>hi</p>", headers={"content-type": "text/html"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert website.robots_allows("https://example.com/", c) is False


def test_check_marks_leads_and_rescore(account_id, monkeypatch):
    monkeypatch.setattr(website, "fetch_site",
                        lambda url: ("ok", "Locally owned since 1990") if url else ("no_site", ""))
    with Session(engine) as db:
        stats = website.check_unchecked(db, account_id)
        assert stats["ok"] == 1 and stats["no_site"] == 1
        site = db.scalars(select(Lead).where(Lead.account_id == account_id, Lead.business_name == "Has Site")).one()
        assert site.score is None and "Locally owned" in scoring.describe(site)
        nosite = db.scalars(select(Lead).where(Lead.account_id == account_id, Lead.business_name == "No Site")).one()
        assert nosite.score == 6  # kept: nothing new to score with


def test_account_limit_stops_before_overall_cap(account_id):
    with Session(engine) as db:
        db.add(ApiUsage(account_id=account_id, service="claude_scoring", detail="x", est_cost_usd=14.99))
        db.commit()
        with pytest.raises(places.BudgetExceeded, match="allowance"):
            places.check_budget(db, account_id, 0.035)

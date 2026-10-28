"""Checks Stripe billing with a fake Stripe (no real requests, no charges)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import auth, billing, gmail, nightly, outreach, places, plans
from app.db import engine
from app.main import app
from app.models import Account, Lead, Organization, User


class FakeStripe:
    def __init__(self):
        self.created, self.status = [], "trialing"
        outer = self

        class Prices:
            def list(self, params):
                return NS(data=[])

            def create(self, params):
                outer.created.append(("price", params["lookup_key"], params["unit_amount"]))
                return NS(id=f"price_{params['lookup_key']}")

        class Products:
            def create(self, params):
                return NS(id="prod_1")

        class Sessions:
            def create(self, params):
                outer.checkout = params
                return NS(url="https://checkout.stripe.test/s1")

            def retrieve(self, sid):
                return NS(client_reference_id=outer.checkout["client_reference_id"], customer="cus_1",
                          subscription="sub_1", metadata=outer.checkout["metadata"])

        class Subs:
            def retrieve(self, sid):
                return NS(id="sub_1", status=outer.status)

        class Portal:
            def create(self, params):
                return NS(url="https://billing.stripe.test/p1")

        self.v1 = NS(prices=Prices(), products=Products(), checkout=NS(sessions=Sessions()),
                     subscriptions=Subs(), billing_portal=NS(sessions=Portal()))


@pytest.fixture
def org(monkeypatch):
    fake = FakeStripe()
    billing._price_ids.clear()
    monkeypatch.setattr(billing, "client", lambda: fake)
    monkeypatch.setattr(billing, "configured", lambda: True)
    with Session(engine) as db:
        o = Organization(name="Test: Billing Co", plan="trial", plan_tier="personal")
        db.add(o)
        db.flush()
        a = auth.new_account(db, o.id, "Test: Billing Pub", "37934")
        auth.new_account(db, o.id, "Test: Billing Pub 2")
        u = User(org_id=o.id, account_id=a.id, email="boss@billing.test", role="owner")
        db.add(u)
        db.commit()
        out = {"org": o.id, "acct": a.id, "user": u.id, "fake": fake}
    yield out
    with Session(engine) as db:
        db.execute(delete(Organization).where(Organization.name.like("Test:%")))
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.commit()


def client(uid):
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE, auth.make_session(uid))
    return c


def test_pro_checkout_and_return(org):
    c = client(org["user"])
    page = c.get("/billing").text
    assert "Choose Plug Employ" in page and "$49" in page
    assert "Choose Standard" not in page and "Choose Premium" not in page and "Choose Beta" not in page
    r = c.post("/billing/checkout", data={"tier": "pro"}, follow_redirects=False)
    assert r.headers["location"] == "https://checkout.stripe.test/s1"
    fake = org["fake"]
    assert [c for c in fake.created if c[0] == "price"] == [("price", "pei_pro_49_monthly", 4900)]
    assert "trial_period_days" not in fake.checkout["subscription_data"]
    assert fake.checkout["customer_email"] == "boss@billing.test"
    assert "subscribed to the Plug Employ plan" in c.get("/billing/return?session_id=cs_1").text
    with Session(engine) as db:
        o = db.get(Organization, org["org"])
        assert (o.plan_tier, o.plan, o.billing_status, o.stripe_customer_id) == ("pro", "active", "trialing", "cus_1")
        assert plans.monthly_limit(db, db.get(Account, org["acct"])) == 6.5


def test_old_plans_not_for_sale(org):
    r = client(org["user"]).post("/billing/checkout", data={"tier": "wholesale"}, follow_redirects=False)
    assert "Unknown%20plan" in r.headers["location"]


def test_free_beta_ends_after_30_days(org):
    with Session(engine) as db:
        o = db.get(Organization, org["org"])
        o.plan_tier = "beta"
        a = db.get(Account, org["acct"])
        assert plans.monthly_limit(db, a) == 15.0 and plans.has_email(db, a)
        o.created_at = datetime.now(timezone.utc) - timedelta(days=31)
        db.commit()
        assert plans.monthly_limit(db, a) == 0.0
        with pytest.raises(places.BudgetExceeded, match="free trial has ended"):
            places.check_budget(db, a.id, 0.035)
        o.plan = "active"  # subscribed: no trial limit
        db.commit()
        assert plans.monthly_limit(db, a) == 15.0


def test_signup_starts_on_beta():
    with Session(engine) as db:
        u = auth.sign_in(db, "new@beta.test", "New Person", "Test: Beta Signup Co")
        o = db.get(Organization, u.org_id)
        assert (o.plan, o.plan_tier) == ("trial", "beta")
        db.delete(u)
        db.commit()


def test_try_link_signs_up_on_free_search(monkeypatch):
    from app import main
    monkeypatch.setattr(gmail, "configured", lambda: True)
    monkeypatch.setattr(auth, "start_login", lambda company: "https://accounts.google.test/")
    monkeypatch.setattr(auth, "google_identity", lambda s, c: ({"email": "new@free.test", "name": "Free Person"}, "Test: Free Co"))
    c = TestClient(app, follow_redirects=False)
    assert "one free lead search" in c.get("/try").text
    c.get("/auth/google?company=Test:%20Free%20Co&offer=free")
    assert c.cookies.get(main.OFFER_COOKIE) == "free"
    assert c.get("/auth/callback?state=s&code=c").headers["location"] == "/setup"
    with Session(engine) as db:
        u = db.scalars(select(User).where(User.email == "new@free.test")).one()
        o = db.get(Organization, u.org_id)
        assert (o.plan, o.plan_tier, o.signup_offer) == ("trial", "free", "free")
        db.delete(u)
        db.commit()


def test_free_signup_tracker_on_beta_page(org):
    from tests.conftest import login_as
    with Session(engine) as db:
        o = db.get(Organization, org["org"])
        o.plan_tier, o.signup_offer = "free", "free"
        db.commit()
    c = client(org["user"])
    c.post("/offer/seen")
    c.post("/billing/checkout", data={"tier": "pro"}, follow_redirects=False)
    with Session(engine) as db:
        o = db.get(Organization, org["org"])
        assert o.offer_seen_at and o.checkout_started_at
        owner_org = Organization(name="Test: Owner Co", plan="active", plan_tier="owner")
        db.add(owner_org)
        db.flush()
        owner_acct = auth.new_account(db, owner_org.id, "Test: Owner Pub")
        db.commit()
        owner_acct_id = owner_acct.id
    owner = TestClient(app)
    login_as(owner, owner_acct_id)
    page = owner.get("/beta").text
    assert "Free search sign-ups" in page and "Test: Billing Co" in page and "boss@billing.test" in page
    assert "Clicked Subscribe" in page


def test_privacy_and_terms_pages():
    c = TestClient(app)
    assert "We never sell your data" in c.get("/privacy").text and "Limited Use" in c.get("/privacy").text
    assert "$49 per month" in c.get("/terms").text
    assert 'href="/terms"' in c.get("/try").text


def test_existing_beta_tester_keeps_beta_through_try_link():
    with Session(engine) as db:
        u = auth.sign_in(db, "tester@beta.test", "Tester", "Test: Beta Tester Co")
        again = auth.sign_in(db, "tester@beta.test", "Tester", "Test: Other Co", "free")
        assert again.id == u.id and db.get(Organization, u.org_id).plan_tier == "beta"
        db.delete(u)
        db.commit()


def test_free_search_is_one_search_ever_then_offer(org):
    from app.models import ApiUsage
    c = client(org["user"])
    with Session(engine) as db:
        o = db.get(Organization, org["org"])
        o.plan_tier = "free"
        a = db.get(Account, org["acct"])
        a.setup_done_at = datetime.now(timezone.utc)
        assert plans.trial_ends(o) is None
        assert "1 ZIP code." in plans.territory_error(plans.PLANS["free"], ["37934", "37919"])
        assert "1 business type." in plans.territory_error(plans.PLANS["free"], ["37934"], ["Florist", "Bakery"])
        db.commit()
    setup_page = c.get("/setup").text
    assert "one ZIP code" in setup_page and "Suggest from my description" not in setup_page
    assert "Run my free search" in c.get("/setup/first").text
    assert "subscribe-modal" not in c.get("/leads?sort=score").text  # no search yet
    with Session(engine) as db:
        # a search last month still counts: the free search is once, not once a month
        db.add(ApiUsage(account_id=org["acct"], service="overture_search", detail="Florist in 37934", est_cost_usd=0,
                        created_at=datetime.now(timezone.utc) - timedelta(days=40)))
        db.commit()
        assert plans.usage(db, db.get(Account, org["acct"]))["searches_left"] == 0
        with pytest.raises(places.BudgetExceeded, match="used your free search"):
            places.check_search(db, org["acct"])
    assert "subscribe-modal" not in c.get("/leads?sort=score").text  # searched, but leads not ready yet
    from app.models import NightlyRun
    done = datetime.now(timezone.utc) - timedelta(seconds=5)
    with Session(engine) as db:
        db.add(NightlyRun(account_id=org["acct"], searches=0, finished_at=done - timedelta(hours=1)))  # a nightly run with no search
        db.add(NightlyRun(account_id=org["acct"], searches=1, added=12, finished_at=done))
        db.commit()
        assert plans.first_leads_at(db, org["acct"]) == done
    page = c.get("/leads?sort=score").text
    assert "subscribe-modal" in page and "Subscribe for $49/month" in page
    assert str(int((done + timedelta(seconds=30)).timestamp() * 1000)) in page  # shows 30 seconds after leads are ready
    assert "subscribe-modal" not in c.get("/billing").text
    c.cookies.set("subscribe_later", "1")
    assert "subscribe-modal" not in c.get("/leads?sort=score").text


def test_standard_plan_has_no_gmail(org, monkeypatch):
    monkeypatch.setattr(gmail, "configured", lambda: True)
    with Session(engine) as db:
        a = db.get(Account, org["acct"])
        a.gmail_token_enc, a.setup_done_at = "x", datetime.now(timezone.utc)
        lead = Lead(account_id=a.id, business_name="Test Lead", score=9)
        db.add(lead)
        db.commit()
        assert not plans.has_email(db, a)
        assert gmail.check_account(db, a.id) == {"checked": 0, "emailed": 0, "follow_ups": 0}
        with pytest.raises(ValueError, match="Plug Employ plan"):
            outreach.draft_for_lead(db, a, lead)
        lead_id = lead.id
    c = TestClient(app, follow_redirects=False)
    c.cookies.set(auth.SESSION_COOKIE, auth.make_session(org["user"]))
    assert "included in the" in c.get("/business").text
    assert "Plug%20Employ%20plan" in c.get("/gmail/connect?agree=1").headers["location"]
    assert "Draft an email" not in c.get(f"/leads/{lead_id}").text
    c.post("/nightly", data={"enabled": "1", "run_hour": "2", "searches_per_night": "100"})
    with Session(engine) as db:
        assert nightly.get_settings(db, org["acct"]).searches_per_night == 20


def test_cancelled_subscription_pauses_searches(org):
    c = client(org["user"])
    c.post("/billing/checkout", data={"tier": "pro"}, follow_redirects=False)
    c.get("/billing/return?session_id=cs_1")
    org["fake"].status = "canceled"
    with Session(engine) as db:
        o = db.get(Organization, org["org"])
        assert billing.sync(db, o) == "canceled" and o.plan == "cancelled"
        assert plans.monthly_limit(db, db.get(Account, org["acct"])) == 0.0


def test_publishers_cannot_reach_billing(org):
    with Session(engine) as db:
        u = User(org_id=org["org"], account_id=org["acct"], email="pub@billing.test", role="publisher")
        db.add(u)
        db.commit()
        uid = u.id
    assert client(uid).get("/billing", follow_redirects=False).status_code == 303

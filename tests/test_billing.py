"""Checks Stripe billing with a fake Stripe (no real requests, no charges)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
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

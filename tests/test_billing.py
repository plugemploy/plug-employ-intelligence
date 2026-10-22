"""Checks Stripe billing with a fake Stripe (no real requests, no charges)."""

from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app import auth, billing, plans
from app.db import engine
from app.main import app
from app.models import Account, Organization, User


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


def test_beta_checkout_and_return(org):
    c = client(org["user"])
    assert "Choose Beta" in c.get("/billing").text
    r = c.post("/billing/checkout", data={"tier": "beta"}, follow_redirects=False)
    assert r.headers["location"] == "https://checkout.stripe.test/s1"
    fake = org["fake"]
    assert ("price", "pei_beta_monthly", 4900) in fake.created
    assert fake.checkout["subscription_data"]["trial_period_days"] == 30
    assert fake.checkout["customer_email"] == "boss@billing.test"
    assert "subscribed to the Beta plan" in c.get("/billing/return?session_id=cs_1").text
    with Session(engine) as db:
        o = db.get(Organization, org["org"])
        assert (o.plan_tier, o.plan, o.billing_status, o.stripe_customer_id) == ("beta", "active", "trialing", "cus_1")
        assert plans.monthly_limit(db, db.get(Account, org["acct"])) == 8.0


def test_wholesale_charges_per_publisher(org):
    client(org["user"]).post("/billing/checkout", data={"tier": "wholesale"}, follow_redirects=False)
    assert org["fake"].checkout["line_items"][0]["quantity"] == 2


def test_cancelled_subscription_pauses_searches(org):
    c = client(org["user"])
    c.post("/billing/checkout", data={"tier": "personal"}, follow_redirects=False)
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

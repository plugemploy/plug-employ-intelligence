"""Checks Stripe billing with a fake Stripe (no real requests, no charges)."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
import stripe
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import auth, billing, gmail, nightly, outreach, places, plans
from app.db import engine
from app.main import app
from app.models import Account, Lead, Organization, User
from app.search_jobs import reasons_after_free_search as real_reasons_after_free_search  # before conftest stubs it


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

            def retrieve(self, sid):  # real Stripe objects: they don't act like dicts (.get fails)
                return stripe.StripeObject.construct_from(dict(
                    client_reference_id=outer.checkout["client_reference_id"], customer="cus_1",
                    subscription="sub_1", metadata=outer.checkout["metadata"]), "sk_test")

        class Subs:
            def retrieve(self, sid):
                return stripe.StripeObject.construct_from(dict(id="sub_1", status=outer.status), "sk_test")

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
    assert "Try a free lead search" in c.get("/try").text
    c.get("/auth/google?company=Test:%20Free%20Co&offer=free&agree=yes")
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
    csv_text = owner.get("/beta.csv").text
    assert "FREE SEARCH SIGN-UPS" in csv_text
    line = [l for l in csv_text.split("FREE SEARCH SIGN-UPS")[1].splitlines() if l.startswith("Test: Billing Co")][0]
    assert "boss@billing.test" in line and str(date.today()) in line  # saw the offer / clicked Subscribe dates


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
        assert "3 ZIP codes." in plans.territory_error(plans.PLANS["free"], ["37934", "37919", "37922", "37923"])
        assert "2 business types." in plans.territory_error(plans.PLANS["free"], ["37934"], ["Florist", "Bakery", "Dentist"])
        db.commit()
    setup_page = c.get("/setup").text
    assert "3 ZIP codes" in setup_page and "Suggest from my description" not in setup_page
    assert "maxPick = 2" in setup_page and "of 2 selected" in setup_page and "Select all" not in setup_page
    # three business types: an error, but the description and ZIP aren't lost
    r = c.post("/setup", data={"description": "Test: wedding mag", "zips": "37934",
                               "industries": ["Florist", "Bakery", "Dentist"]}, follow_redirects=False)
    assert "2%20business%20types" in r.headers["location"]
    with Session(engine) as db:
        a = db.get(Account, org["acct"])
        assert (a.business_description, a.zip_list) == ("Test: wedding mag", ["37934"])
    assert "Run my free search" in c.get("/setup/first").text
    assert "subscribe-modal" not in c.get("/leads?sort=score").text  # no search yet
    with Session(engine) as db:
        # searches last month still count: the free search (up to 3 ZIPs x 2 types) is once, not once a month
        for z in ("37934", "37919", "37922"):
            for ind in ("Florist", "Bakery"):
                db.add(ApiUsage(account_id=org["acct"], service="overture_search", detail=f"{ind} in {z}", est_cost_usd=0,
                                created_at=datetime.now(timezone.utc) - timedelta(days=40)))
        db.commit()
        assert plans.usage(db, db.get(Account, org["acct"]))["searches_left"] == 0
        with pytest.raises(places.BudgetExceeded, match="used your free search"):
            places.check_search(db, org["acct"])
    usage_page = c.get("/usage").text
    assert "You've used your free search." in usage_page and "Subscribe for $49/month" in usage_page
    assert "paused until the 1st" not in usage_page and "this month" not in usage_page.split("free search")[0][-200:]
    page = c.get("/leads?sort=score").text
    assert "subscribe-modal" not in page and "subscribe-banner" not in page  # searched, but leads not ready yet
    from app.models import NightlyRun
    done = datetime.now(timezone.utc) - timedelta(seconds=5)
    with Session(engine) as db:
        db.add(NightlyRun(account_id=org["acct"], searches=0, finished_at=done - timedelta(hours=1)))  # a nightly run with no search
        db.add(NightlyRun(account_id=org["acct"], searches=1, added=12, finished_at=done))
        db.commit()
        assert plans.first_leads_at(db, org["acct"]) == done
    page = c.get("/leads?sort=score").text
    assert "subscribe-modal" in page and "Subscribe for $49/month" in page
    assert str(int((done + timedelta(seconds=15)).timestamp() * 1000)) in page  # shows 15 seconds after leads are ready
    assert "subscribe-banner" in page
    assert "about 6 years" in page and "30-day money-back guarantee" in page  # payback line and guarantee
    assert "subscribe-modal" not in c.get("/billing").text and "subscribe-banner" not in c.get("/billing").text
    c.cookies.set("subscribe_later", "1")
    page = c.get("/leads?sort=score").text
    assert "subscribe-modal" not in page and "subscribe-banner" in page  # "Maybe later" hides only the pop-up
    # the soft VA line under their leads, and "I'm interested" saved for Beta results
    assert "Short on time to call them?" not in page  # no leads yet
    with Session(engine) as db:
        db.add(Lead(account_id=org["acct"], business_name="Test VA Lead", score=8))
        db.commit()
    assert "1 lead ready. Short on time to call them?" in c.get("/leads?sort=score").text
    assert "We'll email you at" in c.post("/va/interest").text
    assert "We'll email you at" in c.get("/leads?sort=score").text
    with Session(engine) as db:
        assert db.get(Organization, org["org"]).va_interest_at is not None


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


def missing(what):
    import stripe
    return stripe.InvalidRequestError(f"No such {what}", param="id", code="resource_missing")


def test_switch_to_live_key_unlinks_test_subscriptions(org):
    c = client(org["user"])
    c.post("/billing/checkout", data={"tier": "pro"}, follow_redirects=False)
    c.get("/billing/return?session_id=cs_1")
    fake = org["fake"]
    with Session(engine) as db:  # a second company whose subscription is fine
        other = Organization(name="Test: Live Co", plan="active", plan_tier="pro", stripe_subscription_id="sub_live",
                             stripe_customer_id="cus_live", billing_status="active")
        db.add(other)
        db.commit()
        other_id = other.id

    def retrieve(sid):
        if sid == "sub_1":
            raise missing("subscription")
        return NS(id=sid, status="past_due")
    fake.v1.subscriptions.retrieve = retrieve
    with Session(engine) as db:
        billing.sync_all(db)
        o, live = db.get(Organization, org["org"]), db.get(Organization, other_id)
        assert (o.stripe_subscription_id, o.stripe_customer_id, o.plan) == ("", "", "cancelled")
        assert (live.billing_status, live.plan) == ("past_due", "active")  # still checked after the bad one

    # checkout with a customer Stripe no longer knows starts fresh with the email
    with Session(engine) as db:
        db.get(Organization, org["org"]).stripe_customer_id = "cus_test_only"
        db.commit()
    create = fake.v1.checkout.sessions.create
    def picky_create(params):
        if params.get("customer") == "cus_test_only":
            raise missing("customer")
        return create(params)
    fake.v1.checkout.sessions.create = picky_create
    r = c.post("/billing/checkout", data={"tier": "pro"}, follow_redirects=False)
    assert r.headers["location"] == "https://checkout.stripe.test/s1"
    assert fake.checkout["customer_email"] == "boss@billing.test" and "customer" not in fake.checkout


def test_publishers_cannot_reach_billing(org):
    with Session(engine) as db:
        u = User(org_id=org["org"], account_id=org["acct"], email="pub@billing.test", role="publisher")
        db.add(u)
        db.commit()
        uid = u.id
    assert client(uid).get("/billing", follow_redirects=False).status_code == 303


def test_free_search_shows_top_20_and_locks_the_rest(org, monkeypatch):
    from app import search_jobs
    from app.models import LeadSignal, NightlyRun
    with Session(engine) as db:
        db.get(Organization, org["org"]).plan_tier = "free"
        db.get(Account, org["acct"]).setup_done_at = datetime.now(timezone.utc)
        leads = [Lead(account_id=org["acct"], business_name=f"Test Lead {i:02}", score=10 - i // 3, industry="Florist",
                      zip_code="37934", website=f"https://lead{i}.test") for i in range(26)]
        db.add_all(leads)
        db.add(NightlyRun(account_id=org["acct"], searches=6, added=26, finished_at=datetime.now(timezone.utc)))
        db.flush()
        db.add(LeadSignal(account_id=org["acct"], lead_id=leads[24].id, kind="opening", text="Opened a new shop."))
        db.commit()
        top, hidden = leads[0].id, leads[25].id
        assert len(plans.locked_ids(db, db.get(Account, org["acct"]))) == 6
    c = client(org["user"])
    page = c.get("/leads?sort=score").text
    assert "Test Lead 00" in page and "Test Lead 19" in page
    assert "Test Lead 20" not in page and "Test Lead 25" not in page and "lead25.test" not in page  # not even in the HTML
    assert "6 more leads</b> from your search" in page and "1</b> has a reason to reach out right now" in page
    assert "connect your Gmail" in page
    assert c.get(f"/leads/{top}").status_code == 200
    r = c.get(f"/leads/{hidden}", follow_redirects=False)
    assert r.status_code == 303 and "Subscribe%20to%20see%20this%20lead" in r.headers["location"]
    assert c.post(f"/leads/{hidden}/status", data={"status": "Contacted"}, follow_redirects=False).status_code == 303
    assert "Opened a new shop" not in c.get("/dashboard").text  # locked lead's reason stays hidden
    # deleting a visible lead doesn't unlock another
    c.post(f"/leads/{top}/delete")
    assert "Test Lead 20" not in c.get("/leads?sort=score").text
    # reasons to reach out are checked right after a free search, locked leads too
    spawned = []
    monkeypatch.setattr(search_jobs, "spawn", lambda fn, *a: spawned.append(a))
    with Session(engine) as db:
        real_reasons_after_free_search(db, org["acct"])
        assert spawned == [(org["acct"],)]
        db.get(Organization, org["org"]).plan_tier = "pro"  # subscribed: everything opens, no extra check
        db.commit()
        real_reasons_after_free_search(db, org["acct"])
        assert len(spawned) == 1
    page = c.get("/leads?sort=score").text
    assert "Test Lead 25" in page and "more leads</b> from your search" not in page


def test_free_search_runs_in_background_and_list_refreshes(org, monkeypatch):
    from app import search_jobs
    monkeypatch.setattr(search_jobs, "spawn", lambda fn, *a: None)  # leave it "running"
    with Session(engine) as db:
        db.get(Organization, org["org"]).plan_tier = "free"
        a = db.get(Account, org["acct"])
        a.setup_done_at, a.target_zips, a.industries = datetime.now(timezone.utc), "37934, 37919", "Florist"
        db.add(Lead(account_id=a.id, business_name="Test Found Early"))
        db.commit()
    c = client(org["user"])
    r = c.post("/setup/first", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/leads?search=")
    assert c.post("/setup/first", follow_redirects=False).headers["location"] == r.headers["location"]  # no second search
    page = c.get("/leads?sort=score").text
    assert 'hx-trigger="every 4s"' in page and "Finding your leads" in page and "Test Found Early" in page
    with Session(engine) as db:
        job = search_jobs.latest(db, org["acct"])
        job.status, job.finished_at = "done", datetime.now(timezone.utc)
        db.commit()
    page = c.get("/leads?sort=score").text
    assert 'hx-trigger="every 4s"' not in page and "Finding your leads" not in page
    assert 'id="offer-ready"' in page and "subscribe-banner" in page  # the refreshed list tells the page to reload


def test_reason_leads_on_top_and_page_live_while_checking(org):
    from app import signals
    from app.models import LeadSignal
    with Session(engine) as db:
        db.get(Organization, org["org"]).plan_tier = "free"
        db.get(Account, org["acct"]).setup_done_at = datetime.now(timezone.utc)
        best = Lead(account_id=org["acct"], business_name="Test Best Score", score=10)
        news = Lead(account_id=org["acct"], business_name="Test Has News", score=6)
        db.add_all([best, news])
        db.flush()
        db.add(LeadSignal(account_id=org["acct"], lead_id=news.id, kind="opening", text="Opening a second shop in May."))
        db.commit()
    c = client(org["user"])
    signals.checking[org["acct"]] = [3, 10]
    try:
        page = c.get("/leads?sort=score").text
        assert page.index("Test Has News") < page.index("Test Best Score")  # reason first, even with a lower score
        assert "🔔 Reason to reach out" in page and "Opening a second shop in May." in page
        assert "Looking for reasons to reach out" in page and "checked 3 of 10" in page and 'hx-trigger="every 4s"' in page
    finally:
        signals.checking.pop(org["acct"], None)
    assert 'hx-trigger="every 4s"' not in c.get("/leads?sort=score").text


def test_free_search_starts_when_setup_is_saved(org, monkeypatch):
    from app import search_jobs
    monkeypatch.setattr(search_jobs, "spawn", lambda fn, *a: None)  # leave it "running"
    with Session(engine) as db:
        db.get(Organization, org["org"]).plan_tier = "free"
        db.commit()
    c = client(org["user"])
    r = c.post("/setup", data={"description": "Test: home mag", "zips": "37934, 37919", "industries": ["Florist"]},
               follow_redirects=False)
    assert r.headers["location"].startswith("/leads?search=")  # no "Run my free search" click
    with Session(engine) as db:
        assert search_jobs.latest(db, org["acct"]).status == "running"


def test_owner_deletes_a_test_company(org):
    from tests.conftest import login_as
    with Session(engine) as db:
        db.add(Lead(account_id=org["acct"], business_name="Test Gone Lead"))
        owner_org = Organization(name="Test: Owner Deletes", plan="active", plan_tier="owner")
        db.add(owner_org)
        db.flush()
        owner_acct = auth.new_account(db, owner_org.id, "Test: Owner Deletes Pub")
        paying = Organization(name="Test: Paying Co", plan="active", plan_tier="pro", stripe_subscription_id="sub_x")
        db.add(paying)
        db.commit()
        owner_acct_id, owner_org_id, paying_id = owner_acct.id, owner_org.id, paying.id
    c = client(org["user"])
    assert c.post("/beta/delete", data={"org_id": org["org"]}, follow_redirects=False).status_code == 404  # owner only
    owner = TestClient(app)
    login_as(owner, owner_acct_id)
    assert "Delete test company" in owner.get("/beta").text
    r = owner.post("/beta/delete", data={"org_id": owner_org_id}, follow_redirects=False)
    assert "can%E2%80%99t%20be%20deleted" in r.headers["location"]  # not your own company
    r = owner.post("/beta/delete", data={"org_id": paying_id}, follow_redirects=False)
    assert "Cancel%20it%20in%20Stripe%20first" in r.headers["location"]
    r = owner.post("/beta/delete", data={"org_id": org["org"]}, follow_redirects=False)
    assert "Deleted%20Test%3A%20Billing%20Co" in r.headers["location"]
    with Session(engine) as db:
        assert db.get(Organization, org["org"]) is None and db.get(User, org["user"]) is None
        assert db.get(Account, org["acct"]) is None
        assert not db.scalars(select(Lead).where(Lead.business_name == "Test Gone Lead")).all()

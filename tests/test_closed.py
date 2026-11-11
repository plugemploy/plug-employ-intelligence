"""Checks the "May be closed" flag (fake websites, no network)."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app import closed, plans, scoring, website
from app.db import engine
from app.main import app
from app.models import Account, Lead
from tests.conftest import login_as, on_plan


@pytest.fixture
def ids():
    with Session(engine) as db:
        a = Account(name="Test: Closed")
        db.add(a)
        db.flush()
        leads = {
            "gone": Lead(account_id=a.id, business_name="Gone Gym", website="https://gone.test", score=9,
                         website_checked_at=datetime.now(timezone.utc)),
            "open": Lead(account_id=a.id, business_name="Open Spa", website="https://open.test", score=6,
                         website_checked_at=datetime.now(timezone.utc)),
            "phone": Lead(account_id=a.id, business_name="Phone Only Roofing", phone="865-555-0100", score=5),
            "sold": Lead(account_id=a.id, business_name="Client Gone", website="https://client.test", score=8,
                         status="Sold", website_checked_at=datetime.now(timezone.utc)),
        }
        db.add_all(leads.values())
        db.commit()
        out = {"a": a.id, **{k: l.id for k, l in leads.items()}}
    on_plan(out["a"], "pro")
    yield out
    with Session(engine) as db:
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.commit()


def test_only_strong_signs_count_as_a_gone_website():
    request = httpx.Request("GET", "https://x.test")
    dns = httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known", request=request)
    ssl = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", request=request)
    refused = httpx.ConnectError("[Errno 61] Connection refused", request=request)
    gone = httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))
    blocked = httpx.HTTPStatusError("403", request=request, response=httpx.Response(403, request=request))
    assert website.dead_status(dns) == "dead_dns"
    assert website.dead_status(gone) == "dead_404"
    assert website.dead_status(ssl) == website.dead_status(refused) == website.dead_status(blocked) == ""
    assert website.dead_status(httpx.ReadTimeout("slow", request=request)) == ""
    assert website.dead_status(page_url="https://www.hugedomains.com/domain_profile.cfm?d=gone") == "dead_parked"
    assert website.dead_status(page_url="https://gone.test", text="GoneGym.com is for sale! This domain may be for sale.") == "dead_parked"
    assert website.dead_status(page_url="https://open.test", text="Welcome to Open Spa. Book your facial today.") == ""


def test_a_gone_website_is_flagged_only_on_a_later_second_look(ids):
    with Session(engine) as db:
        lead = db.get(Lead, ids["gone"])
        closed.saw_site(lead, "dead_dns")
        assert lead.dead_since and lead.closed_reason == ""  # first sighting: wait
        closed.saw_site(lead, "dead_dns")
        assert lead.closed_reason == ""  # moments later: still waiting
        lead.dead_since -= closed.RECHECK_AFTER
        closed.saw_site(lead, "error")  # a slow site says nothing either way
        assert lead.closed_reason == "" and lead.dead_since
        closed.saw_site(lead, "dead_dns")
        assert lead.closed_reason == "Its website address no longer exists" and lead.closed_flagged_at
        closed.saw_site(lead, "ok")  # the site came back
        assert lead.closed_reason == "" and lead.dead_since is None
        sold = db.get(Lead, ids["sold"])
        sold.dead_since = datetime.now(timezone.utc) - timedelta(hours=1)
        closed.saw_site(sold, "dead_404")
        assert sold.closed_reason == ""  # customers are never flagged


def test_check_account_double_checks_and_sweeps(ids, monkeypatch):
    monkeypatch.setattr(website, "site_status", lambda url: "dead_parked" if "gone" in url else "ok")
    with Session(engine) as db:
        assert closed.check_account(db, ids["a"]) == {"checked": 2, "flagged": 0, "cleared": 0, "removed": 0}  # not the sold one
        db.get(Lead, ids["gone"]).dead_since -= closed.RECHECK_AFTER
        db.commit()
        assert closed.check_account(db, ids["a"], only_double_checks=True)["removed"] == 1
        lead = db.get(Lead, ids["gone"])
        assert lead.closed_reason.startswith("Its website is now") and lead.website_status == "dead_parked"
        assert lead.deleted_at  # nobody had worked on it: moved to Deleted leads
        assert closed.check_account(db, ids["a"])["checked"] == 0  # nothing due again until next week/month
        assert db.get(Lead, ids["phone"]).closed_reason == ""  # no website is never a sign


def test_website_saying_it_closed_flags_it(ids):
    with Session(engine) as db:
        lead = db.get(Lead, ids["open"])
        lead.website_text = "After 22 wonderful years we have closed our doors. Thank you, Knoxville!"
        result = scoring.LeadScore(score=3, reason="Closed.", contact_name="", contact_title="",
                                   closed_sign="we have closed our doors")
        scoring.save(db, lead, result, 0.001)
        assert lead.closed_reason == 'Website says: "we have closed our doors"'
        scoring.save(db, lead, result.model_copy(update={"closed_sign": ""}), 0.001)
        assert lead.closed_reason == ""


def test_flagged_leads_sort_last_and_answers_work(ids):
    with Session(engine) as db:
        lead = db.get(Lead, ids["gone"])
        lead.closed_reason, lead.closed_flagged_at = "Its website address no longer exists", datetime.now(timezone.utc)
        db.commit()
    c = TestClient(app)
    login_as(c, ids["a"])
    page = c.get("/leads?sort=score").text
    assert "⚠️ May be closed" in page and "Its website address no longer exists" in page
    assert page.index("Open Spa") < page.index("Phone Only Roofing") < page.index("Gone Gym")  # top score, but last
    assert "⚠️ May be closed" in c.get(f"/leads/{ids['gone']}").text
    c.post(f"/leads/{ids['gone']}/still-open", data={"back": "lead"})
    with Session(engine) as db:
        lead = db.get(Lead, ids["gone"])
        assert lead.closed_reason == "" and lead.open_confirmed_at
        closed.flag(lead, "Its website address no longer exists")
        assert lead.closed_reason == ""  # "Still open" is final
    c.post(f"/leads/{ids['phone']}/closed")
    with Session(engine) as db:
        lead = db.get(Lead, ids["phone"])
        assert lead.deleted_at and lead.closed_reported_at
    assert "Phone Only Roofing" not in c.get("/leads?sort=score").text


def test_flagged_leads_never_take_a_free_top_spot(ids, monkeypatch):
    monkeypatch.setattr(plans, "FREE_SHOWN", 2)
    with Session(engine) as db:
        db.get(Account, ids["a"])
        lead = db.get(Lead, ids["gone"])
        lead.closed_reason = "Its website is gone (page not found)"
        from app.models import Organization
        db.get(Organization, db.get(Account, ids["a"]).org_id).plan_tier = "free"
        db.commit()
        locked = plans.locked_ids(db, db.get(Account, ids["a"]))
    assert ids["gone"] in locked and ids["open"] not in locked and ids["sold"] not in locked


def test_didnt_load_is_down_but_ssl_and_refusals_are_not():
    request = httpx.Request("GET", "https://x.test")
    assert website.down_status(httpx.ReadTimeout("slow", request=request)) == "down"
    assert website.down_status(httpx.ConnectError("[Errno 61] Connection refused", request=request)) == "down"
    server = httpx.HTTPStatusError("503", request=request, response=httpx.Response(503, request=request))
    assert website.down_status(server) == "down"
    ssl = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", request=request)
    blocked = httpx.HTTPStatusError("403", request=request, response=httpx.Response(403, request=request))
    assert website.down_status(ssl) == website.down_status(blocked) == ""


def test_down_twice_a_day_apart_removes_untouched_leads_only(ids):
    with Session(engine) as db:
        lead, worked = db.get(Lead, ids["gone"]), db.get(Lead, ids["open"])
        worked.status = "Contacted"
        for l in (lead, worked):
            closed.saw_site(l, "down")
            assert l.closed_reason == closed.DOWN_ONCE and not l.deleted_at  # labelled, checked again tomorrow
            closed.saw_site(l, "down")
            assert not l.deleted_at  # minutes later doesn't count
            l.down_since -= closed.DOWN_RECHECK
            closed.saw_site(l, "down")
            assert l.closed_reason == closed.DOWN_TWICE
        assert lead.deleted_at and not worked.deleted_at  # someone is talking to that one: flagged only
        closed.saw_site(worked, "ok")
        assert worked.closed_reason == "" and worked.down_since is None


def test_unsure_places_without_a_website_are_removed(ids):
    with Session(engine) as db:
        unsure = Lead(account_id=ids["a"], business_name="Maybe Lawn Care", phone="865-555-0111", source_confidence=0.3)
        sure = Lead(account_id=ids["a"], business_name="Sure Lawn Care", phone="865-555-0112", source_confidence=0.8)
        called = Lead(account_id=ids["a"], business_name="Called Lawn Care", phone="865-555-0113", source_confidence=0.3,
                      status="Contacted")
        db.add_all([unsure, sure, called])
        db.commit()
        assert closed.remove_unsure_without_site(db, ids["a"]) == 1
        assert unsure.deleted_at and unsure.closed_reason == closed.NO_SITE
        assert not sure.deleted_at and not called.deleted_at and not db.get(Lead, ids["phone"]).deleted_at


def test_removed_leads_show_why_and_restoring_keeps_them(ids):
    with Session(engine) as db:
        lead = db.get(Lead, ids["gone"])
        lead.dead_since = datetime.now(timezone.utc) - closed.RECHECK_AFTER
        closed.saw_site(lead, "dead_dns")
        db.commit()
        assert lead.deleted_at
    c = TestClient(app)
    login_as(c, ids["a"])
    assert "Gone Gym" not in c.get("/leads").text
    assert "Removed as probably closed: Its website address no longer exists" in c.get("/leads/deleted").text
    c.post(f"/leads/{ids['gone']}/restore")
    with Session(engine) as db:
        lead = db.get(Lead, ids["gone"])
        assert not lead.deleted_at and lead.open_confirmed_at and lead.closed_reason == ""
        closed.saw_site(lead, "dead_dns")
        lead.dead_since -= closed.RECHECK_AFTER
        closed.saw_site(lead, "dead_dns")
        assert not lead.deleted_at and lead.closed_reason == ""  # restored means open, for good


def test_may_be_closed_leads_are_not_sent_to_close(ids):
    from app import close_crm
    with Session(engine) as db:
        lead, other = db.get(Lead, ids["gone"]), db.get(Lead, ids["open"])
        lead.phone, other.phone = "865-555-0120", "865-555-0121"
        lead.status = "Contacted"  # kept, but flagged
        closed.flag(lead, closed.DOWN_TWICE)
        db.commit()
        assert not close_crm.sendable(lead) and close_crm.sendable(other)
        unsent = {l.id for l in db.scalars(close_crm.unsent_query(ids["a"]))}
        assert ids["open"] in unsent and ids["gone"] not in unsent


def test_a_website_taken_over_by_gambling_spam_loses_its_link_but_stays(ids):
    spam = "Slot Gacor Hari Ini - Situs Slot Online Terpercaya Maxwin. Daftar sekarang, deposit pulsa."
    assert website.dead_status(page_url="https://oldobgyn.test", text=spam) == "dead_hijacked"
    assert website.dead_status(page_url="https://vet.test", text="Our vets host a casino night fundraiser!") == ""
    with Session(engine) as db:
        lead = db.get(Lead, ids["gone"])
        lead.source_confidence = 0.3  # would be removed if it had simply never had a website
        closed.saw_site(lead, "dead_hijacked")
        assert (lead.website, lead.bad_website, lead.website_status) == ("", "https://gone.test", "no_site")
        assert lead.closed_reason == "" and not lead.deleted_at
        db.commit()
        assert closed.remove_unsure_without_site(db, ids["a"]) == 0
    c = TestClient(app)
    login_as(c, ids["a"])
    assert "Old website removed (it showed gambling ads): https://gone.test" in c.get(f"/leads/{ids['gone']}").text


def test_claude_spotting_a_website_that_isnt_theirs_removes_the_link(ids):
    with Session(engine) as db:
        lead = db.get(Lead, ids["open"])
        lead.website_text = "Best online casino bonuses. Owner: Mr. Spam since 1999."
        result = scoring.LeadScore(score=3, reason="Website is a gambling site.", contact_name="Mr. Spam",
                                   contact_title="Owner", founded_year=1999, site_not_theirs="gambling ads")
        scoring.save(db, lead, result, 0.001)
        assert (lead.website, lead.bad_website, lead.bad_website_why) == ("", "https://open.test", "it showed gambling ads")
        assert lead.contact_name == "" and lead.founded_year is None  # nothing taken from a spam page
        assert not lead.deleted_at and lead.closed_reason == ""

"""Accessibility basics: skip link, named form fields, accessibility page."""

import re

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db import engine
from app.main import app
from app.models import Account, Lead
from tests.conftest import login_as


def unnamed_fields(html: str) -> list[str]:
    """Form fields with no label, aria-label or aria-labelledby (hidden fields and buttons don't need one)."""
    labelled = set(re.findall(r'<label[^>]*\bfor="([^"]+)"', html))
    body = re.sub(r"<label\b.*?</label>", "", html, flags=re.S)  # fields wrapped in a label are named by it
    bad = []
    for tag in re.findall(r"<(?:input|select|textarea)\b[^>]*>", body):
        if re.search(r'type="(hidden|submit|button)"', tag) or "aria-label" in tag:
            continue
        m = re.search(r'\bid="([^"]+)"', tag)
        if not (m and m.group(1) in labelled):
            bad.append(tag)
    return bad


def test_pages_have_skip_link_and_named_fields():
    with Session(engine) as db:
        acc = Account(name="Test: a11y")
        db.add(acc); db.flush()
        lead = Lead(account_id=acc.id, business_name="Test Bakery", industry="Bakery", zip_code="37934")
        db.add(lead); db.commit()
        acc_id, lead_id = acc.id, lead.id
    c = TestClient(app)
    login_as(c, acc_id)
    for path in ["/leads", f"/leads/{lead_id}", "/business", "/search", "/nightly", "/leads/new", "/dashboard", "/setup"]:
        html = c.get(path).text
        assert 'href="#main"' in html and 'id="main"' in html, path
        assert unnamed_fields(html) == [], path
    with Session(engine) as db:
        db.delete(db.get(Lead, lead_id)); db.delete(db.get(Account, acc_id)); db.commit()


def test_accessibility_page_and_public_pages():
    c = TestClient(app)
    page = c.get("/accessibility")
    assert page.status_code == 200 and "WCAG" in page.text
    for path in ["/login", "/signup", "/try", "/privacy", "/terms"]:
        html = c.get(path).text
        assert "<header>" in html and "<footer" in html and 'href="/accessibility"' in html, path
        assert unnamed_fields(html) == [], path

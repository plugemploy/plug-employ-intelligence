"""Checks that the old Render address forwards to the app's own domain."""

from fastapi.testclient import TestClient

from app import main
from app.main import app


def test_old_address_forwards(monkeypatch):
    monkeypatch.setattr(main, "APP_BASE_URL", "https://app.example.com")
    c = TestClient(app, base_url="https://sample-app.onrender.com")
    r = c.get("/leads?sort=score", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "https://app.example.com/leads?sort=score"
    assert c.get("/health", follow_redirects=False).status_code == 200  # Render's own check is left alone
    assert TestClient(app).get("/health").status_code == 200  # other addresses are untouched


def test_no_forward_while_still_on_render(monkeypatch):
    monkeypatch.setattr(main, "APP_BASE_URL", "https://sample-app.onrender.com")
    c = TestClient(app, base_url="https://sample-app.onrender.com")
    assert c.get("/login", follow_redirects=False).status_code != 307

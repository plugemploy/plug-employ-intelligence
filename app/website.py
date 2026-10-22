"""Read a business's own homepage to sharpen its score.

Rules: only the homepage, only if the site's robots.txt allows it, one request per site,
identified with our own user agent. No logins, no workarounds.
"""

import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Lead

USER_AGENT = "PlugEmployIntelligence/0.1 (local business lead research)"
MAX_BYTES = 500_000
MAX_TEXT = 6000  # characters kept per site (homepage + About page); keeps Claude costs tiny


ABOUT_WORDS = ("about", "team", "our-story", "ourstory", "meet", "staff", "leadership", "owner", "who-we-are", "doctor")


class _TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self._skip, self.links = [], 0, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href:
                self.links.append(href)
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextOnly()
    p.feed(html)
    return re.sub(r"\s+", " ", " ".join(p.parts)).strip()[:MAX_TEXT]


def find_about_link(html: str, page_url: str) -> str:
    """The site's own About / Team page, if the homepage links to one (same website only)."""
    from urllib.parse import urljoin, urlsplit
    p = _TextOnly()
    p.feed(html)
    home = urlsplit(page_url).hostname or ""
    for href in p.links:
        full = urljoin(page_url, href.split("#")[0])
        parts = urlsplit(full)
        if parts.scheme in ("http", "https") and (parts.hostname or "").removeprefix("www.") == home.removeprefix("www.") \
                and any(w in parts.path.lower() for w in ABOUT_WORDS) and full.rstrip("/") != page_url.rstrip("/"):
            return full
    return ""


def robots_allows(url: str, client: httpx.Client) -> bool:
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        r = client.get(robots_url)
    except httpx.HTTPError:
        return True  # no robots.txt reachable: the standard treats that as allowed
    if r.status_code in (401, 403):
        return False
    if r.status_code >= 400:
        return True
    rp = RobotFileParser()
    rp.parse(r.text.splitlines())
    return rp.can_fetch(USER_AGENT, url)


def fetch_site(url: str) -> tuple[str, str, str]:
    """Returns (status, text, about_url): the homepage, plus its About/Team page when linked and allowed.
    Kept separate so tests can replace it."""
    if not url.startswith(("http://", "https://")):
        return "no_site", "", ""
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=10, follow_redirects=True) as client:
        if not robots_allows(url, client):
            return "blocked", "", ""
        try:
            r = client.get(url)
            r.raise_for_status()
        except httpx.HTTPError:
            return "error", "", ""
        if "html" not in r.headers.get("content-type", ""):
            return "error", "", ""
        html = r.text[:MAX_BYTES]
        text = html_to_text(html)
        about = find_about_link(html, str(r.url))
        if about and robots_allows(about, client):
            try:
                a = client.get(about)
                if a.status_code == 200 and "html" in a.headers.get("content-type", ""):
                    return "ok", (text[: MAX_TEXT // 2] + "\n[About page] " + html_to_text(a.text[:MAX_BYTES]))[:MAX_TEXT], about
            except httpx.HTTPError:
                pass
        return "ok", text, ""


def check_unchecked(db: Session, account_id: int, limit: int = 100) -> dict:
    """Read the homepage of leads not checked yet. Clears their score so Claude re-scores with it."""
    stats = {"ok": 0, "no_site": 0, "blocked": 0, "error": 0}
    leads = db.scalars(select(Lead).where(Lead.account_id == account_id, Lead.website_checked_at.is_(None))
                       .order_by(Lead.score.desc().nulls_first(), Lead.id).limit(limit)).all()
    for lead in leads:
        status, text, about = fetch_site(lead.website or "")
        lead.website_status, lead.website_text, lead.website_about_url = status, text, about
        lead.website_checked_at = datetime.now(timezone.utc)
        if status == "ok":
            lead.score = None  # re-score with the website facts
        db.commit()
        stats[status] += 1
    return stats

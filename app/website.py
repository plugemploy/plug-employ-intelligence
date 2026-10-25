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


NEWS_WORDS = ("news", "events", "event", "blog", "announcement", "whats-new", "what-s-new", "press", "calendar",
              "specials", "promotions", "updates")
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


def looks_garbled(text: str) -> bool:
    """Binary or wrongly-encoded pages decode into replacement and control characters."""
    if not text:
        return False
    bad = sum(1 for ch in text[:5000] if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\t\n\r"))
    return bad >= 20 and bad > len(text[:5000]) * 0.02


def html_to_text(html: str) -> str:
    if looks_garbled(html):
        return ""
    p = _TextOnly()
    p.feed(html.replace("\x00", ""))
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", " ".join(p.parts))
    return re.sub(r"\s+", " ", text).strip()[:MAX_TEXT]


def find_about_link(html: str, page_url: str, words: tuple[str, ...] = ABOUT_WORDS) -> str:
    """The site's own About / Team page (or another page whose address has one of `words`), if the homepage
    links to one (same website only)."""
    from urllib.parse import urljoin, urlsplit
    p = _TextOnly()
    p.feed(html)
    home = urlsplit(page_url).hostname or ""
    for href in p.links:
        full = urljoin(page_url, href.split("#")[0])
        parts = urlsplit(full)
        if parts.scheme in ("http", "https") and (parts.hostname or "").removeprefix("www.") == home.removeprefix("www.") \
                and any(w in parts.path.lower() for w in words) and full.rstrip("/") != page_url.rstrip("/"):
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


def full_url(url: str) -> str:
    """A website as people type it ("example.com", "www.example.com ") as a full address, or "" if it isn't one."""
    url = (url or "").strip()
    if not url or " " in url or "@" in url or "." not in url:
        return ""
    return url if url.lower().startswith(("http://", "https://")) else "https://" + url


def fetch_site(url: str) -> tuple[str, str, str]:
    """Returns (status, text, about_url): the homepage, plus its About/Team page when linked and allowed.
    Kept separate so tests can replace it."""
    url = full_url(url)
    if not url:
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
        if not text:
            return "error", "", ""  # unreadable page
        about = find_about_link(html, str(r.url))
        if about and robots_allows(about, client):
            try:
                a = client.get(about)
                if a.status_code == 200 and "html" in a.headers.get("content-type", ""):
                    return "ok", (text[: MAX_TEXT // 2] + "\n[About page] " + html_to_text(a.text[:MAX_BYTES]))[:MAX_TEXT], about
            except httpx.HTTPError:
                pass
        return "ok", text, ""


def fetch_news(url: str) -> tuple[str, str]:
    """Returns (text, news_url): the homepage plus its News / Events / Blog page when linked and allowed, for
    spotting timely reasons to reach out. Same rules as fetch_site. Kept separate so tests can replace it."""
    url = full_url(url)
    if not url:
        return "", ""
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=10, follow_redirects=True) as client:
        if not robots_allows(url, client):
            return "", ""
        try:
            r = client.get(url)
            r.raise_for_status()
        except httpx.HTTPError:
            return "", ""
        if "html" not in r.headers.get("content-type", ""):
            return "", ""
        html = r.text[:MAX_BYTES]
        text = html_to_text(html)
        news = find_about_link(html, str(r.url), NEWS_WORDS)
        if text and news and robots_allows(news, client):
            try:
                n = client.get(news)
                if n.status_code == 200 and "html" in n.headers.get("content-type", ""):
                    return (text[: MAX_TEXT // 2] + "\n[News/Events page] " + html_to_text(n.text[:MAX_BYTES]))[:MAX_TEXT], news
            except httpx.HTTPError:
                pass
        return text, ""


def check_unchecked(db: Session, account_id: int, limit: int = 100) -> dict:
    """Read the homepage of leads not checked yet. Clears their score so Claude re-scores with it."""
    stats = {"ok": 0, "no_site": 0, "blocked": 0, "error": 0}
    leads = db.scalars(select(Lead).where(Lead.account_id == account_id, Lead.deleted_at.is_(None), Lead.website_checked_at.is_(None))
                       .order_by(Lead.score.desc().nulls_first(), Lead.id).limit(limit)).all()
    for lead in leads:
        try:
            status, text, about = fetch_site(lead.website or "")
        except Exception:  # one odd website must never stop the rest
            status, text, about = "error", "", ""
        lead.website_status, lead.website_text, lead.website_about_url = status, text, about
        lead.website_checked_at = datetime.now(timezone.utc)
        if status == "ok":
            lead.score = None  # re-score with the website facts
        db.commit()
        stats[status] += 1
    return stats

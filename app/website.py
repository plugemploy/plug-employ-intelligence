"""Read a business's own homepage to sharpen its score.

Rules: only the homepage, only if the site's robots.txt allows it, one request per site,
identified with our own user agent. No logins, no workarounds.
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import closed, limits, phones
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
            if href.lower().startswith("tel:"):  # the site's own "call us" link: noted so it's found first
                digits = phones.key(href[4:])
                if digits:
                    self.parts.append(f" {phones.TEL_MARK}{digits[:3]}-{digits[3:6]}-{digits[6:]}] ")
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


# A listed website that's gone for good (a strong sign the business may be closed). Slow, refused, blocked or
# certificate-error sites don't count: plenty of open businesses have those.
PARKED_WORDS = ("domain is for sale", "domain may be for sale", "buy this domain", "this domain is for sale",
                "domain has expired", "this domain is parked", "parked free", "domain name is for sale",
                "inquire about this domain", "make an offer on this domain", "this webpage is parked")
PARKING_HOSTS = ("hugedomains.com", "sedo.com", "sedoparking.com", "dan.com", "afternic.com", "parkingcrew.net",
                 "bodis.com", "domainmarket.com", "undeveloped.com", "parklogic.com", "above.com")
# A lapsed domain bought by a spammer: the business's old address now shows gambling ads (often Indonesian slot
# sites). Two different words are needed, so one passing mention never counts.
SPAM_WORDS = ("slot gacor", "judi", "togel", "situs slot", "slot online", "maxwin", "rtp slot", "rtp live", "sbobet",
              "pragmatic play", "casino online", "online casino", "sports betting", "deposit pulsa", "scatter hitam",
              "free spins", "poker online", "sabung ayam", "link alternatif", "bandar togel", "slot88", "gacor",
              "no deposit bonus", "betting odds")
NO_SUCH_NAME = ("name or service not known", "nodename nor servname", "getaddrinfo failed", "no address associated",
                "temporary failure in name resolution", "name does not resolve", "[errno -2]", "[errno 8]", "[errno 11001]")


def dead_status(error: Exception | None = None, page_url: str = "", text: str = "") -> str:
    """ "dead_dns" (the address no longer exists), "dead_404" (the homepage is gone), "dead_parked" (a "domain
    for sale" page) or "dead_hijacked" (gambling spam on the old address), else "". Pass the error from fetching the homepage, or the page it landed on and its text."""
    if isinstance(error, httpx.ConnectError):
        msg = str(error).lower()
        if "ssl" not in msg and "certificate" not in msg and any(w in msg for w in NO_SUCH_NAME):
            return "dead_dns"
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (404, 410):
        return "dead_404"
    host = (urlsplit(page_url).hostname or "").removeprefix("www.")
    if any(host == h or host.endswith("." + h) for h in PARKING_HOSTS):
        return "dead_parked"
    low = text[:3000].lower()
    if len(low) < 3000 and any(w in low for w in PARKED_WORDS):  # parking pages are short
        return "dead_parked"
    if looks_hijacked(text):
        return "dead_hijacked"
    return ""


def looks_hijacked(text: str) -> bool:
    low = (text or "").lower()
    return sum(1 for w in SPAM_WORDS if w in low) >= 2


def down_status(error: Exception | None) -> str:
    """ "down" when the website didn't load at all (timed out, refused, or a server error), else "". Certificate
    problems and "not allowed" answers don't count: the site is there."""
    if isinstance(error, httpx.TimeoutException):
        return "down"
    if isinstance(error, httpx.ConnectError):
        msg = str(error).lower()
        return "" if "ssl" in msg or "certificate" in msg else "down"
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code >= 500:
        return "down"
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


@limits.website
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
        except httpx.HTTPError as exc:
            return dead_status(exc) or down_status(exc) or "error", "", ""
        if "html" not in r.headers.get("content-type", ""):
            return "error", "", ""
        html = r.text[:MAX_BYTES]
        text = html_to_text(html)
        if dead := dead_status(page_url=str(r.url), text=text):
            return dead, "", ""
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


@limits.website
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


@limits.website
def read_site(url: str) -> tuple[str, str, str, str, str]:
    """One visit for a new lead: (status, text, about_url, news_text, news_url). text is the homepage plus its
    About/Team page (for scoring, like fetch_site); news_text is the homepage plus its News/Events page (for reasons
    to reach out, like fetch_news). The homepage is fetched once. Same rules. Kept separate so tests can replace it."""
    url = full_url(url)
    if not url:
        return "no_site", "", "", "", ""
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=10, follow_redirects=True) as client:
        if not robots_allows(url, client):
            return "blocked", "", "", "", ""
        try:
            r = client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            return dead_status(exc) or down_status(exc) or "error", "", "", "", ""
        if "html" not in r.headers.get("content-type", ""):
            return "error", "", "", "", ""
        html = r.text[:MAX_BYTES]
        text = html_to_text(html)
        if dead := dead_status(page_url=str(r.url), text=text):
            return dead, "", "", "", ""
        if not text:
            return "error", "", "", "", ""  # unreadable page

        def extra(link: str, label: str) -> str:
            if not link or not robots_allows(link, client):
                return ""
            try:
                page = client.get(link)
                if page.status_code == 200 and "html" in page.headers.get("content-type", ""):
                    return (text[: MAX_TEXT // 2] + f"\n[{label}] " + html_to_text(page.text[:MAX_BYTES]))[:MAX_TEXT]
            except httpx.HTTPError:
                pass
            return ""
        about, news = find_about_link(html, str(r.url)), find_about_link(html, str(r.url), NEWS_WORDS)
        about_text, news_text = extra(about, "About page"), extra(news, "News/Events page")
        return "ok", about_text or text, about if about_text else "", news_text or text, news if news_text else ""


@limits.website
def site_status(url: str) -> str:
    """A light look at the homepage only, for the "May be closed" checks: "ok", "no_site", "blocked", "error" or a
    dead_* status. Same rules as fetch_site. Kept separate so tests can replace it."""
    url = full_url(url)
    if not url:
        return "no_site"
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=10, follow_redirects=True) as client:
        if not robots_allows(url, client):
            return "blocked"
        try:
            r = client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            return dead_status(exc) or down_status(exc) or "error"
        if "html" not in r.headers.get("content-type", ""):
            return "error"
        return dead_status(page_url=str(r.url), text=html_to_text(r.text[:MAX_BYTES])) or "ok"


PARALLEL = 8  # websites read at once (each is a different site, so this is still one request at a time per site)


def _read(url: str) -> tuple[str, str, str]:
    try:
        return fetch_site(url)
    except Exception:  # one odd website must never stop the rest
        return "error", "", ""


def check_unchecked(db: Session, account_id: int, limit: int = 100) -> dict:
    """Read the homepage of leads not checked yet, several sites at once; each is saved as soon as it's read.
    Clears their score so Claude re-scores with it."""
    stats = {"ok": 0, "no_site": 0, "blocked": 0, "error": 0, "down": 0, "dead": 0}
    leads = db.scalars(select(Lead).where(Lead.account_id == account_id, Lead.deleted_at.is_(None), Lead.website_checked_at.is_(None))
                       .order_by(Lead.score.desc().nulls_first(), Lead.id).limit(limit)).all()
    if not leads:
        return stats
    with ThreadPoolExecutor(min(PARALLEL, len(leads))) as pool:
        reading = {pool.submit(_read, lead.website or ""): lead for lead in leads}  # only URLs go to the threads
        for future in as_completed(reading):
            lead, (status, text, about) = reading[future], future.result()
            lead.website_status, lead.website_text, lead.website_about_url = status, text, about
            lead.website_checked_at = datetime.now(timezone.utc)
            if status == "ok":
                lead.score = None  # re-score with the website facts
                phones.from_site(lead)
            closed.saw_site(lead, status)
            db.commit()
            stats["dead" if status.startswith("dead") else status] += 1
    return stats

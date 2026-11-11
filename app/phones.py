"""A second phone number from the business's own website (map data is sometimes out of date).

When the website lists a local number that isn't the lead's, it's saved as Other phone, so the caller has a second
number to try. The main phone stays the map listing's: a check of real leads showed many websites belong to a
salon-suite building, a franchise head office or a loan officer elsewhere, so the website's number isn't always the
business's own. A lead with no phone gets the website's as its phone. The website's first "call us" (tel:) link is
used, else the first number on the page. 800-type numbers and numbers labelled fax are never used. Done once, when
the website is first read.
"""

import re

from app.models import Lead

TOLL_FREE = {"800", "888", "877", "866", "855", "844", "833"}
TEL_MARK = "[phone: "  # website.html_to_text writes tel: links like this, so they come first
_NUMBER = re.compile(r"(?<![\d-])(?:\+?1[\s.\-]?)?\(?([2-9]\d{2})\)?[\s.\-]{0,2}([2-9]\d{2})[\s.\-]([0-9]{4})(?![\d-])")
_FAX = re.compile(r"\bfax\b|\bf\s*[:.]\s*$|\bfacsimile\b", re.I)


def key(phone: str) -> str:
    """The 10-digit US number, or "" if it isn't one."""
    digits = "".join(c for c in phone or "" if c.isdigit())
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def stored(digits: str) -> str:
    """Saved the way Overture's numbers are (+1 and ten digits)."""
    return "+1" + digits


def on_site(text: str) -> list[str]:
    """Phone numbers in the website text, tel: links first, then in page order; fax numbers left out."""
    tel, plain = [], []
    for m in _NUMBER.finditer(text or ""):
        before = text[max(0, m.start() - 25):m.start()]
        if _FAX.search(re.split(r"\d", before)[-1]):  # the words since the previous number
            continue
        n = "".join(m.groups())
        (tel if before.endswith(TEL_MARK) else plain).append(n)
    out = []
    for n in tel + plain:
        if n not in out:
            out.append(n)
    return out


def from_site(lead: Lead) -> bool:
    """Fill Other phone (and maybe swap in the website's number) from the website text already read. True if changed."""
    if lead.other_phone:
        return False  # already done
    found = on_site(lead.website_text)
    ours = key(lead.phone)
    if not found or ours in found:
        return False  # nothing listed, or the website confirms the number
    local = [n for n in found if n[:3] not in TOLL_FREE]
    if not local:
        return False  # only a national line: the local number stays
    site = stored(local[0])
    if not ours:
        lead.phone, lead.phone_source = site, "website"
    else:
        lead.other_phone, lead.other_phone_source = site, "website"
    return True

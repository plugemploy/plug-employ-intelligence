"""Checks Other phone from the business's website (no network)."""

from types import SimpleNamespace

from app import close_crm, phones, website


def lead(phone="+18655550100", text="", **kw):
    return SimpleNamespace(phone=phone, other_phone="", phone_source="", status="New", close_lead_id="", source="",
                           website_text=text, **kw)


def test_finds_numbers_tel_links_first_and_skips_fax():
    html = ('<p>Phone: (865) 555-0111 Fax: 865.555.0112</p><a href="tel:+1-865-555-0113">Call us</a>'
            '<p>F: 865-555-0114</p><p>Est. 1998, suite 2024</p>')
    text = website.html_to_text(html)
    assert phones.on_site(text) == ["8655550113", "8655550111"]


def test_a_different_local_number_becomes_other_phone():
    l = lead(text="Call us today at (865) 555-0199. Free quotes.")
    assert phones.from_site(l)
    assert (l.phone, l.other_phone) == ("+18655550100", "+18655550199")  # the map's number stays main
    assert not phones.from_site(l)  # done once


def test_same_number_or_only_a_national_line_changes_nothing():
    for text in ("Call 865-555-0100 or 865-555-0177", "Call 1-800-555-0123 any time", "No numbers here"):
        l = lead(text=text)
        assert not phones.from_site(l) and l.other_phone == ""


def test_a_lead_without_a_phone_gets_the_websites():
    l = lead(phone="", text="Book: 972-555-0144")
    assert phones.from_site(l) and (l.phone, l.phone_source, l.other_phone) == ("+19725550144", "website", "")


def test_both_numbers_go_to_close():
    l = SimpleNamespace(business_name="Two Numbers Plumbing", score=None, score_reason="", website="", address="",
                        zip_code="75032", phone="+19725550100", other_phone="+19725550199", contact_name="",
                        contact_title="", email="")
    assert [p["phone"] for p in close_crm.lead_body(l, "s")["contacts"][0]["phones"]] == ["+19725550100", "+19725550199"]

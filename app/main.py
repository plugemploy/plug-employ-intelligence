"""Plug Employ Intelligence: lead finding, scoring and tracking for local ad sellers."""

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db import check_database, get_session
import anthropic
import httpx

from app import auth, billing, catalog, client_import, close_crm, dashboard, plans, digest, gmail, local_pages, nightly, outreach, overture, places, results, scoring, search_jobs, signals
from app.auth import ACCOUNT_COOKIE, current_account, current_user, require_admin
from app import setup as setup_mod
from app import website as site_check
from app.config import ANTHROPIC_API_KEY, APP_BASE_URL, CONTACT_EMAIL, COOKIE_SECURE
from app.dedupe import find_duplicate
from app.models import OUTCOMES, STATUSES, WOULD_PAY, WOULD_PAY_OLD, Account, BetaSurvey, Feedback, FollowUp, Lead, LeadNote, LeadSignal, NightlyRun, Organization, SearchJob, User, WatchedPage

INDUSTRIES = places.INDUSTRIES

@asynccontextmanager
async def lifespan(app):
    sched = nightly.start_scheduler()  # overnight lead search
    yield
    sched.shutdown(wait=False)


app = FastAPI(title="Plug Employ Intelligence", lifespan=lifespan)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals["close_sendable"] = close_crm.sendable
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.middleware("http")
async def forward_old_address(request: Request, call_next):
    """Visits to the old Render address go to the app's own domain (sign-in only works there)."""
    host = request.url.hostname or ""
    if host.endswith(".onrender.com") and ".onrender.com" not in APP_BASE_URL and request.url.path != "/health":
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"{APP_BASE_URL}{request.url.path}{query}", status_code=307)
    return await call_next(request)



@app.exception_handler(auth.LoginRequired)
def login_required(request: Request, exc: auth.LoginRequired):
    if request.headers.get("HX-Request"):
        return HTMLResponse("", headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


def render(request, name, db, account, **ctx):
    user = getattr(request.state, "user", None)
    accounts = auth.visible_accounts(db, user) if user else []
    org = db.get(Organization, user.org_id) if user else None
    job = search_jobs.for_bar(db, account.id) if account else None
    show_costs = bool(org and org.plan_tier == "owner")  # dollar amounts are for the app owner only
    survey_due = bool(user and account and org and survey_is_due(db, org, account, request))
    banner, subscribe_at = subscribe_offer(db, org, user, account, request) if org else (False, None)
    return templates.TemplateResponse(
        request, name,
        {"account": account, "accounts": accounts, "user": user, "org": org, "statuses": STATUSES,
         "today": date.today(), "search_job": job, "show_costs": show_costs, "survey_due": survey_due,
         "subscribe_banner": banner, "subscribe_at": subscribe_at, "pro": plans.PLANS["pro"],
         "max_pick": plans.plan_for(db, account).get("max_industries") if account else None,  # industry picker limit
         "would_pay": WOULD_PAY, "would_pay_labels": {**WOULD_PAY_OLD, **WOULD_PAY}, **ctx},
    )


LEADS_FILTER_COOKIE = "leads_filters"
SURVEY_AFTER_DAYS = 25  # the beta lasts 30 days; ask a few days before it ends
SURVEY_LATER_COOKIE = "survey_later"


def survey_is_due(db: Session, org: Organization, account: Account, request: Request) -> bool:
    """Beta companies get the survey once per account, from day 25, unless they said "later" recently."""
    if org.plan_tier != "beta" or org.created_at is None or request.cookies.get(SURVEY_LATER_COOKIE):
        return False
    if datetime.now(timezone.utc) < org.created_at + timedelta(days=SURVEY_AFTER_DAYS):
        return False
    return not db.scalar(select(func.count()).select_from(BetaSurvey).where(BetaSurvey.account_id == account.id))


SUBSCRIBE_AFTER = timedelta(seconds=15)  # after the free search's leads are ready, before the $49 offer
SUBSCRIBE_LATER_COOKIE = "subscribe_later"  # "Maybe later" hides the offer for a day


def subscribe_offer(db: Session, org: Organization, user, account, request: Request) -> tuple[bool, int | None]:
    """The $49 offer for a Free search company once its leads are ready: (show the banner, when the pop-up opens
    in ms since 1970 for the page's timer, or None). The banner stays; "Maybe later" only hides the pop-up."""
    if org.plan_tier != "free" or not (user and user.is_admin and account) or request.url.path.startswith("/billing"):
        return False, None
    ready = plans.first_leads_at(db, account.id)
    if not ready:
        return False, None
    popup = None if request.cookies.get(SUBSCRIBE_LATER_COOKIE) else int((ready + SUBSCRIBE_AFTER).timestamp() * 1000)
    return True, popup


PING_SECONDS = 30  # the page pings this often while it's open and in use
NEW_VISIT_AFTER = timedelta(minutes=30)


@app.post("/ping")
def ping(db: Session = Depends(get_session), user: User = Depends(current_user)):
    """Count time in the app: the gap since the last ping (when it's short), and a new visit after a quiet spell."""
    now = datetime.now(timezone.utc)
    gap = (now - user.last_seen_at).total_seconds() if user.last_seen_at else None
    if gap is None or gap > NEW_VISIT_AFTER.total_seconds():
        user.visits += 1
    elif gap <= 2 * PING_SECONDS:  # longer gaps mean the tab sat idle or hidden: not counted
        user.active_seconds += int(gap)
    user.last_seen_at = now
    db.commit()
    return HTMLResponse("")


def activity(people: list) -> dict:
    """Time in the app, visits and last seen for a company's people (Beta results)."""
    secs = sum(u.active_seconds or 0 for u in people)
    seen = [u.last_seen_at for u in people if u.last_seen_at]
    h, m = divmod(round(secs / 60), 60)
    agreed = [u.terms_agreed_at for u in people if u.terms_agreed_at]
    return dict(active_seconds=secs, active=(f"{h}h {m}m" if h else f"{m} min") if secs else "",
                visits=sum(u.visits or 0 for u in people), last_seen=max(seen) if seen else None,
                agreed=min(agreed) if agreed else None)


@app.get("/offer/teaser")
def offer_teaser(request: Request, style: str = Query("banner"), db: Session = Depends(get_session),
                 account=Depends(current_account)):
    """What's waiting in a free search company's area: "44 more Roofing leads near you and 1,200 other businesses"."""
    industry = (account.industry_list or [""])[0]
    have = db.scalar(select(func.count()).select_from(Lead).where(Lead.account_id == account.id, Lead.deleted_at.is_(None))) or 0
    counts = overture.waiting(account.zip_list, industry, catalog.ALL, have) if industry and account.zip_list else (0, 0)
    return templates.TemplateResponse(request, "_teaser.html", {"counts": counts, "industry": industry, "style": style})


@app.post("/offer/seen")
def offer_seen(db: Session = Depends(get_session), user: User = Depends(current_user)):
    """The $49 offer was just shown (banner on page load, pop-up by its timer). The first time is kept."""
    org = db.get(Organization, user.org_id)
    if org.offer_seen_at is None:
        org.offer_seen_at = datetime.now(timezone.utc)
        db.commit()
    return HTMLResponse("")


@app.post("/va/interest")
def va_interest(request: Request, db: Session = Depends(get_session), user: User = Depends(current_user)):
    """Free search "I'm interested" in us hiring and training a VA to call their leads. Shown on Beta results."""
    org = db.get(Organization, user.org_id)
    org.va_interest_at = org.va_interest_at or datetime.now(timezone.utc)
    db.commit()
    return HTMLResponse(f'<div id="va-offer" class="mt-4 rounded-xl border bg-white p-4 text-sm text-slate-700">'
                        f"✅ Thanks! We'll email you at {escape(user.email)} within a day.</div>")


def parse_day(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def can_draft(db: Session, account: Account | None) -> bool:
    return bool(account and plans.has_email(db, account) and gmail.can_compose(account))


def get_lead(db: Session, account: Account | None, lead_id: int) -> Lead:
    """Load a lead only if it belongs to the current account."""
    lead = db.get(Lead, lead_id)
    if account is None or lead is None or lead.account_id != account.id:
        raise HTTPException(404, "Lead not found")
    if lead.id in plans.locked_ids(db, account):
        raise LeadLocked()
    return lead


class LeadLocked(Exception):
    """A Free search lead past the top plans.FREE_SHOWN: subscribe to open it."""


@app.exception_handler(LeadLocked)
def lead_locked(request: Request, exc: LeadLocked):
    msg = "Subscribe to see this lead. The $49 plan unlocks every lead from your search."
    if request.headers.get("HX-Request"):
        return HTMLResponse("", headers={"HX-Redirect": f"/leads?msg={quote(msg)}"})
    return RedirectResponse(f"/leads?msg={quote(msg)}", status_code=303)


@app.get("/")
def home():
    return RedirectResponse("/leads")


# ---- Sign in / sign up ---------------------------------------------------------------------

def page(request, name, **ctx):
    return templates.TemplateResponse(request, name, {"user": None, "account": None, "accounts": [], **ctx})


@app.get("/login")
def login_page(request: Request, msg: str = ""):
    return page(request, "login.html", message=msg, ready=gmail.configured())


@app.get("/signup")
def signup_page(request: Request, msg: str = ""):
    return page(request, "signup.html", message=msg, ready=gmail.configured())


@app.get("/privacy")
def privacy_page(request: Request):
    return page(request, "privacy.html", contact=CONTACT_EMAIL)


@app.get("/terms")
def terms_page(request: Request):
    return page(request, "terms.html", contact=CONTACT_EMAIL, pro=plans.PLANS["pro"])


OFFER_COOKIE = "offer"  # "free" while someone from the /try link is away at Google signing in
AGREE_COOKIE = "agreed"  # set when the sign-up box was ticked, read back when Google returns


@app.get("/try")
def try_page(request: Request, msg: str = ""):
    """The link in the email campaign: one free search, then the $49 plan."""
    return page(request, "try.html", message=msg, ready=gmail.configured())


@app.get("/auth/google")
def auth_google(company: str = "", offer: str = "", agree: str = ""):
    if not gmail.configured():
        return RedirectResponse("/login?msg=Google%20sign-in%20isn%27t%20set%20up%20yet.", status_code=303)
    if company and not agree:
        back = "/try" if offer == "free" else "/signup"
        return RedirectResponse(f"{back}?msg=Please%20tick%20the%20box%20to%20continue.", status_code=303)
    resp = RedirectResponse(auth.start_login(company), status_code=303)
    if agree:
        resp.set_cookie(AGREE_COOKIE, auth.TERMS_VERSION, max_age=1800, httponly=True, samesite="lax", secure=COOKIE_SECURE)
    if offer == "free":
        resp.set_cookie(OFFER_COOKIE, "free", max_age=1800, httponly=True, samesite="lax", secure=COOKIE_SECURE)
    else:
        resp.delete_cookie(OFFER_COOKIE)
    return resp


@app.get("/auth/callback")
def auth_callback(request: Request, state: str = "", code: str = "", error: str = "", db: Session = Depends(get_session)):
    from urllib.parse import quote
    if error:
        return RedirectResponse(f"/login?msg={quote('Sign-in cancelled.')}", status_code=303)
    tier = "free" if request.cookies.get(OFFER_COOKIE) == "free" else "beta"
    try:
        identity, company = auth.google_identity(state, code)
        user = auth.sign_in(db, identity["email"], identity["name"], company, tier)
        if request.cookies.get(AGREE_COOKIE) == auth.TERMS_VERSION:
            auth.agree(user)
            db.commit()
    except Exception as exc:
        return RedirectResponse(f"/login?msg={quote(str(exc).splitlines()[0])}", status_code=303)
    resp = RedirectResponse("/setup" if company else "/leads", status_code=303)
    resp.set_cookie(auth.SESSION_COOKIE, auth.make_session(user.id), httponly=True, samesite="lax", secure=COOKIE_SECURE,
                    max_age=auth.SESSION_DAYS * 86400)
    resp.delete_cookie(ACCOUNT_COOKIE)
    resp.delete_cookie(OFFER_COOKIE)
    resp.delete_cookie(AGREE_COOKIE)
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    resp.delete_cookie(ACCOUNT_COOKIE)
    return resp


@app.post("/switch-account")
def switch_account(account_id: int = Form(...), db: Session = Depends(get_session), user: User = Depends(current_user)):
    target = db.get(Account, account_id)
    if not user.is_admin or target is None or target.org_id != user.org_id:
        raise HTTPException(404, "Account not found")
    resp = RedirectResponse("/leads", status_code=303)
    resp.set_cookie(ACCOUNT_COOKIE, str(account_id), httponly=True, samesite="lax", secure=COOKIE_SECURE)
    return resp


@app.get("/leads")
def leads_list(request: Request, text: str = Query("", alias="q"), status: str = "", zip_code: str = "", industry: str = "", sort: str = "follow_up", new: str = "",
               hide_contacted: str = "", search: int | None = None, added_from: str = "", added_to: str = "",
               reasons: str = "", export: str = "",
               db: Session = Depends(get_session), account=Depends(current_account)):
    if account and account.setup_done_at is None:
        return RedirectResponse("/setup", status_code=303)
    # Remember the filters so coming back from a lead shows the same list.
    from urllib.parse import urlencode
    filter_keys = ("q", "status", "zip_code", "industry", "sort", "new", "hide_contacted", "added_from", "added_to", "reasons")
    chosen = {k: v for k, v in request.query_params.items() if k in filter_keys and v}
    saved = request.cookies.get(LEADS_FILTER_COOKIE, "")
    if "clear" in request.query_params:
        resp = RedirectResponse("/leads", status_code=303)
        resp.delete_cookie(LEADS_FILTER_COOKIE)
        return resp
    if not request.query_params and saved:
        return RedirectResponse(f"/leads?{saved}", status_code=303)
    leads, last_run, from_search, reason_counts, locked = [], None, None, {}, []
    if account:
        for sig in signals.active(db, account.id, limit=10_000):
            reason_counts[sig.lead_id] = reason_counts.get(sig.lead_id, 0) + 1
        last_run = db.scalars(select(NightlyRun).where(NightlyRun.account_id == account.id)
                              .order_by(NightlyRun.started_at.desc())).first()
        q = (select(Lead).where(Lead.account_id == account.id, Lead.deleted_at.is_(None))
             .options(selectinload(Lead.notes), selectinload(Lead.follow_ups)))
        if status:
            q = q.where(Lead.status == status)
        if zip_code:
            q = q.where(Lead.zip_code == zip_code)
        if industry:
            q = q.where(Lead.industry == industry)
        if text.strip():
            from sqlalchemy import or_
            like = f"%{text.strip()}%"
            noted = select(LeadNote.lead_id).where(LeadNote.account_id == account.id, LeadNote.text.ilike(like))
            q = q.where(or_(Lead.business_name.ilike(like), Lead.contact_name.ilike(like), Lead.address.ilike(like),
                            Lead.industry.ilike(like), Lead.phone.ilike(like), Lead.website.ilike(like),
                            Lead.id.in_(noted)))
        if hide_contacted:
            q = q.where(Lead.last_emailed_at.is_(None))
        if new and last_run:
            q = q.where(Lead.created_at >= last_run.started_at)
        if reasons:
            q = q.where(Lead.id.in_(list(reason_counts) or [0]))
        if search:  # "Show them" after a Find leads search: only the leads it added
            from_search = db.get(SearchJob, search)
            if from_search is None or from_search.account_id != account.id:
                raise HTTPException(404, "Search not found")
            q = q.where(Lead.created_at >= from_search.started_at)
            if from_search.finished_at:
                q = q.where(Lead.created_at <= from_search.finished_at)
        if day := parse_day(added_from):
            q = q.where(Lead.created_at >= datetime.combine(day, datetime.min.time()).astimezone())
        if day := parse_day(added_to):
            q = q.where(Lead.created_at < datetime.combine(day + timedelta(days=1), datetime.min.time()).astimezone())
        leads = list(db.scalars(q.order_by(Lead.business_name)))
        if locked_ids := plans.locked_ids(db, account):  # a Free search: the rest unlock when they subscribe
            leads = [l for l in leads if l.id not in locked_ids]
            locked = sorted(db.scalars(select(Lead).where(Lead.id.in_(locked_ids), Lead.deleted_at.is_(None))),
                            key=lambda l: (-(l.score or 0), l.id))
        if sort == "score":
            leads.sort(key=lambda l: -(l.score or 0))
        elif sort == "newest":
            leads.sort(key=lambda l: l.created_at, reverse=True)
        elif sort == "industry":
            leads.sort(key=lambda l: ((l.industry or "~").lower(), -(l.score or 0)))
        elif sort == "follow_up":
            leads.sort(key=lambda l: l.next_follow_up.due_date if l.next_follow_up else date.max)
    if export == "csv":
        return leads_csv(leads)
    industries = sorted({i for i in db.scalars(select(Lead.industry).where(
        Lead.account_id == (account.id if account else 0), Lead.deleted_at.is_(None))) if i}, key=str.lower)
    zips = sorted({z for z in db.scalars(select(Lead.zip_code).where(Lead.account_id == (account.id if account else 0), Lead.deleted_at.is_(None)))})
    unscored = sum(1 for l in leads if l.score is None)
    n_deleted = db.scalar(select(func.count()).select_from(Lead).where(
        Lead.account_id == account.id, Lead.deleted_at.is_not(None))) if account else 0
    resp = render(request, "leads.html", db, account, leads=leads, n_deleted=n_deleted, zips=zips, industries=industries, has_filters=bool(chosen),
                  f_q=text, f_status=status, f_zip=zip_code, f_industry=industry, sort=sort, unscored=unscored, f_new=new, f_hide=hide_contacted, last_run=last_run,
                  from_search=from_search, f_from=added_from, f_to=added_to, reason_counts=reason_counts, f_reasons=reasons,
                  locked=locked, locked_high=sum(1 for l in locked if (l.score or 0) >= 8),
                  locked_reasons=sum(1 for l in locked if l.id in reason_counts),
                  working=scoring.whats_working(db, account.id) if account else {},
                  allow=plans.usage(db, account) if account else None,
                  has_claude_key=bool(ANTHROPIC_API_KEY), has_close=close_crm.enabled_for(db, account), can_draft=can_draft(db, account), message=request.query_params.get("msg", ""))
    if "msg" not in request.query_params:  # a status message page keeps the earlier filters
        if chosen:
            resp.set_cookie(LEADS_FILTER_COOKIE, urlencode(chosen), httponly=True, samesite="lax", secure=COOKIE_SECURE, max_age=30 * 86400)
        else:
            resp.delete_cookie(LEADS_FILTER_COOKIE)
    return resp


def csv_cell(value) -> str:
    """Text for a spreadsheet cell; a leading = + - @ is escaped so Excel can't run it as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def leads_csv(leads: list[Lead]):
    """The leads list as it's filtered and sorted on screen, as a spreadsheet download."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Business", "Contact", "Contact title", "Email", "Phone", "Website", "Address", "ZIP", "Industry",
                "Score", "Why this score", "Status", "Next follow-up", "Last emailed", "Monthly revenue $",
                "Contract months", "Notes", "Date added"])
    for l in leads:
        notes = " | ".join(n.text for n in sorted(l.notes, key=lambda n: n.created_at))
        w.writerow([csv_cell(v) for v in (
            l.business_name, l.contact_name, l.contact_title, l.email, l.phone, l.website, l.address, l.zip_code,
            l.industry, l.score, l.score_reason, l.status,
            l.next_follow_up.due_date if l.next_follow_up else "",
            l.last_emailed_at.date() if l.last_emailed_at else "", l.mrr, l.contract_months, notes,
            l.created_at.date() if l.created_at else "")])
    return HTMLResponse("\ufeff" + buf.getvalue(), media_type="text/csv",  # BOM so Excel reads accents correctly
                        headers={"Content-Disposition": f'attachment; filename="leads-{date.today()}.csv"'})


@app.post("/leads/score")
def score_leads(db: Session = Depends(get_session), account=Depends(current_account)):
    if account is None:
        raise HTTPException(400, "Create an account first")
    if not ANTHROPIC_API_KEY:
        msg = "No Claude key in .env yet."
    else:
        try:
            s = scoring.score_unscored(db, account.id)
            msg = f"Scored {s['scored']} leads."
            if plans.plan_for(db, account)["label"] == "Owner":
                msg = f"Scored {s['scored']} leads for about ${s['cost']:.3f}."
            if s["failed"]:
                msg += f" {s['failed']} couldn't be scored; try again later."
        except places.BudgetExceeded as exc:
            msg = str(exc) + " Scoring stopped."
    from urllib.parse import quote
    return RedirectResponse(f"/leads?sort=score&msg={quote(msg)}", status_code=303)


@app.get("/due")
def due_today(request: Request, db: Session = Depends(get_session), account=Depends(current_account)):
    items = []
    if account:
        items = db.scalars(
            select(FollowUp).join(FollowUp.lead).where(FollowUp.account_id == account.id, FollowUp.done.is_(False),
                                                       Lead.deleted_at.is_(None),
                                   FollowUp.due_date <= date.today())
            .options(selectinload(FollowUp.lead)).order_by(FollowUp.due_date)
        ).all()
    return render(request, "due.html", db, account, items=items)


@app.get("/leads/new")
def new_lead_form(request: Request, db: Session = Depends(get_session), account=Depends(current_account)):
    return render(request, "lead_new.html", db, account, form={})


@app.post("/leads/new")
def create_lead(request: Request, business_name: str = Form(...), address: str = Form(""), zip_code: str = Form(""),
                phone: str = Form(""), website: str = Form(""), industry: str = Form(""), add_anyway: str = Form(""),
                db: Session = Depends(get_session), account=Depends(current_account)):
    if account is None:
        raise HTTPException(400, "Create an account first")
    if not add_anyway:
        dupe = find_duplicate(db, account.id, business_name, zip_code, phone, website)
        if dupe:
            form = dict(business_name=business_name, address=address, zip_code=zip_code, phone=phone,
                        website=website, industry=industry)
            return render(request, "lead_new.html", db, account, form=form, dupe=dupe)
    lead = Lead(account_id=account.id, business_name=business_name.strip(), address=address.strip(),
                zip_code=zip_code.strip(), phone=phone.strip(), website=website.strip(),
                industry=industry.strip(), status="New")
    db.add(lead)
    db.commit()
    return RedirectResponse(f"/leads/{lead.id}", status_code=303)


@app.get("/leads/deleted")
def deleted_leads(request: Request, db: Session = Depends(get_session), account=Depends(current_account)):
    leads = db.scalars(select(Lead).where(Lead.account_id == account.id, Lead.deleted_at.is_not(None))
                       .order_by(Lead.deleted_at.desc())).all() if account else []
    return render(request, "deleted.html", db, account, leads=leads, message=request.query_params.get("msg", ""))


@app.post("/leads/{lead_id}/delete")
def delete_lead(lead_id: int, db: Session = Depends(get_session), account=Depends(current_account)):
    lead = get_lead(db, account, lead_id)
    lead.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return back_to_leads(f"Deleted {lead.business_name}. You can bring it back from Deleted leads.")


@app.post("/leads/{lead_id}/restore")
def restore_lead(lead_id: int, db: Session = Depends(get_session), account=Depends(current_account)):
    lead = get_lead(db, account, lead_id)
    lead.deleted_at = None
    db.commit()
    return RedirectResponse(f"/leads/{lead.id}", status_code=303)


@app.get("/leads/{lead_id}")
def lead_detail(lead_id: int, request: Request, db: Session = Depends(get_session),
                account=Depends(current_account)):
    lead = get_lead(db, account, lead_id)
    return render(request, "lead_detail.html", db, account, lead=lead, reasons=signals.active(db, account.id, lead.id),
                  can_draft=can_draft(db, account), has_close=close_crm.enabled_for(db, account))


@app.post("/leads/{lead_id}/status")
def set_status(lead_id: int, request: Request, status: str = Form(...), reason: str = Form(""),
               mrr: str = Form(""), contract_months: str = Form(""), ad_mgmt_fee: str = Form(""),
               found_elsewhere: str = Form(""),
               db: Session = Depends(get_session), account=Depends(current_account)):
    lead = get_lead(db, account, lead_id)
    if status not in STATUSES:
        raise HTTPException(400, "Unknown status")
    now = datetime.now(timezone.utc)
    just_sold = status == "Sold" and lead.status != "Sold"
    if status in OUTCOMES:
        if lead.status != status:
            lead.outcome_at = now
        lead.outcome_reason = reason.strip()[:500]
    else:
        lead.outcome_reason, lead.outcome_at = "", None
    lead.status = status
    lead.reach(status, now)
    if status == "Sold":
        if found_elsewhere in ("yes", "no", "unsure"):
            lead.found_elsewhere = found_elsewhere
        lead.mrr = money(mrr, lead.mrr)
        lead.ad_mgmt_fee = money(ad_mgmt_fee, lead.ad_mgmt_fee)
        months = money(contract_months, lead.contract_months)
        lead.contract_months = int(months) if months else None
        sold_day = (lead.sold_at or now).astimezone().date()
        set_renewal(db, lead, add_months(sold_day, lead.contract_months) if lead.contract_months else None)
    db.commit()
    return render(request, "_status.html", db, account, lead=lead, saved=True, ask_sale=just_sold)


@app.post("/leads/{lead_id}/draft")
def draft_email(lead_id: int, db: Session = Depends(get_session), account=Depends(current_account)):
    from fastapi.responses import HTMLResponse
    from html import escape
    lead = get_lead(db, account, lead_id)
    try:
        email = outreach.draft_for_lead(db, account, lead)
    except Exception as exc:
        return HTMLResponse(f'<p class="text-sm text-red-700">{escape(str(exc).splitlines()[0])}</p>')
    return HTMLResponse(f'<p class="text-sm text-green-700 mb-1">Saved in your Gmail Drafts ✓ Review it there, then send.</p>'
                        f'<p class="text-sm font-semibold">{escape(email.subject)}</p>'
                        f'<p class="text-sm whitespace-pre-line text-slate-700">{escape(email.body)}</p>')


@app.post("/leads/draft-top")
def draft_top(db: Session = Depends(get_session), account=Depends(current_account)):
    from urllib.parse import quote
    if account is None:
        raise HTTPException(400, "Create an account first")
    if not plans.has_email(db, account):
        msg = plans.EMAIL_PLAN_MSG
    elif not gmail.can_compose(account):
        msg = "Reconnect Gmail on My business to allow drafts."
    else:
        try:
            s = outreach.draft_top(db, account)
            msg = f"Saved {s['drafted']} email drafts in your Gmail. Review and send them from Gmail."
            if s["failed"]:
                msg += f" {s['failed']} couldn't be drafted."
        except places.BudgetExceeded as exc:
            msg = str(exc)
    return RedirectResponse(f"/leads?sort=score&msg={quote(msg)}", status_code=303)


RENEWAL_PREFIX = "Renewal coming up"
RENEWAL_DAYS_BEFORE = 30


def money(raw: str, old):
    """A typed amount like "$1,200" as a number; blank clears it; keep the old value if it isn't a number."""
    raw = raw.replace(",", "").replace("$", "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return old


def add_months(day: date, months: int) -> date:
    import calendar
    y, m = divmod(day.month - 1 + months, 12)
    year, month = day.year + y, m + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def set_renewal(db: Session, lead: Lead, contract_end: date | None) -> None:
    """Keep one open renewal follow-up, 30 days before the contract ends."""
    if contract_end == lead.contract_end:
        return
    lead.contract_end = contract_end
    for f in lead.follow_ups:
        if not f.done and f.task.startswith(RENEWAL_PREFIX):
            db.delete(f)
    if contract_end:
        due = max(date.today(), contract_end - timedelta(days=RENEWAL_DAYS_BEFORE))
        db.add(FollowUp(account_id=lead.account_id, lead_id=lead.id, due_date=due,
                        task=f"{RENEWAL_PREFIX}: contract ends {contract_end:%b %-d, %Y}"))


def back_to_leads(msg: str):
    from urllib.parse import quote
    return RedirectResponse(f"/leads?sort=score&msg={quote(msg)}", status_code=303)


@app.post("/close/send-top")
def close_send_top(count: int = Form(close_crm.TOP_N), db: Session = Depends(get_session), account=Depends(current_account)):
    if not close_crm.enabled_for(db, account):
        return back_to_leads("Close isn't connected for this account.")
    count = count if count in close_crm.SEND_SIZES else close_crm.TOP_N
    try:
        s = close_crm.send_leads(db, close_crm.top_unsent(db, account.id, limit=count), close_crm.client_for(db, account))
    except Exception as exc:
        return back_to_leads(f"Close error: {str(exc).splitlines()[0]}")
    return back_to_leads(f"Sent {s['sent']} leads to Close as Potential. {s['already_in_close']} were already in Close."
                         + (f" {s['failed']} failed." if s["failed"] else ""))


@app.post("/close/send-selected")
def close_send_selected(lead_ids: list[int] = Form(default=[]), db: Session = Depends(get_session),
                        account=Depends(current_account)):
    """Send the leads someone ticked on the leads page (theirs, not yet in Close, with a phone)."""
    if not close_crm.enabled_for(db, account):
        return back_to_leads("Close isn't connected for this account.")
    leads = db.scalars(select(Lead).where(Lead.account_id == account.id, Lead.id.in_(lead_ids or [0]))
                       .order_by(Lead.score.desc().nulls_last())).all()
    leads = [l for l in leads if close_crm.sendable(l)]
    if not leads:
        return back_to_leads("Tick the leads you want to send to Close first.")
    try:
        s = close_crm.send_leads(db, leads, close_crm.client_for(db, account))
    except Exception as exc:
        return back_to_leads(f"Close error: {str(exc).splitlines()[0]}")
    return back_to_leads(f"Sent {s['sent']} leads to Close as Potential. {s['already_in_close']} were already in Close."
                         + (f" {s['failed']} failed." if s["failed"] else ""))


@app.post("/close/sync")
def close_sync(db: Session = Depends(get_session), account=Depends(current_account)):
    if not close_crm.enabled_for(db, account):
        return back_to_leads("Close isn't connected for this account.")
    try:
        s = close_crm.sync_back(db, account.id, close_crm.client_for(db, account))
    except Exception as exc:
        return back_to_leads(f"Close error: {str(exc).splitlines()[0]}")
    return back_to_leads(f"Synced from Close: {s['updated']} leads updated.")


@app.post("/leads/{lead_id}/close")
def close_send_one(lead_id: int, db: Session = Depends(get_session), account=Depends(current_account)):
    from fastapi.responses import HTMLResponse
    from html import escape
    lead = get_lead(db, account, lead_id)
    if not close_crm.enabled_for(db, account):
        return HTMLResponse('<span class="text-sm text-red-700">Close isn\'t connected for this account.</span>')
    try:
        s = close_crm.send_leads(db, [lead], close_crm.client_for(db, account))
    except Exception as exc:
        return HTMLResponse(f'<span class="text-sm text-red-700">{escape(str(exc).splitlines()[0])}</span>')
    msg = "Sent to Close as Potential ✓" if s["sent"] else "Already in Close ✓" if s["already_in_close"] else "Couldn't send."
    return HTMLResponse(f'<span class="text-sm text-green-700">{msg}</span>')


@app.post("/close/connect")
def close_connect(api_key: str = Form(""), db: Session = Depends(get_session), account=Depends(current_account)):
    if account is None:
        raise HTTPException(400, "Create an account first")
    if not plans.has_close(db, account):
        return back_to_business("The Close CRM connection is included in the $49 Plug Employ plan. Switch on the Billing page.")
    try:
        org_name = close_crm.connect(db, account, api_key)
    except ValueError as exc:
        return back_to_business(str(exc))
    except httpx.HTTPError as exc:
        return back_to_business(f"Couldn't reach Close: {str(exc).splitlines()[0]}")
    return back_to_business(f"Connected to Close{f' ({org_name})' if org_name else ''}. Send leads from your Leads page.")


@app.post("/close/disconnect")
def close_disconnect(db: Session = Depends(get_session), account=Depends(current_account)):
    if account:
        close_crm.disconnect(db, account)
    return back_to_business("Close disconnected. Its key was deleted from this app.")


@app.post("/close/settings")
def close_settings(close_auto_send: str = Form(""), db: Session = Depends(get_session), account=Depends(current_account)):
    if account:
        account.close_auto_send = bool(close_auto_send)
        db.commit()
    return RedirectResponse("/nightly?saved=1", status_code=303)


@app.post("/leads/{lead_id}/notes")
def add_note(lead_id: int, request: Request, text: str = Form(...),
             db: Session = Depends(get_session), account=Depends(current_account)):
    lead = get_lead(db, account, lead_id)
    if text.strip():
        db.add(LeadNote(account_id=account.id, lead_id=lead.id, text=text.strip()))
        db.commit()
        db.refresh(lead)
    return render(request, "_notes.html", db, account, lead=lead)


@app.post("/leads/{lead_id}/follow-ups")
def add_follow_up(lead_id: int, request: Request, due_date: date = Form(...), task: str = Form(""),
                  db: Session = Depends(get_session), account=Depends(current_account)):
    lead = get_lead(db, account, lead_id)
    db.add(FollowUp(account_id=account.id, lead_id=lead.id, due_date=due_date, task=task.strip()))
    db.commit()
    db.refresh(lead)
    return render(request, "_follow_ups.html", db, account, lead=lead)


@app.get("/leads/{lead_id}/follow-ups")
def follow_ups_part(lead_id: int, request: Request, db: Session = Depends(get_session), account=Depends(current_account)):
    return render(request, "_follow_ups.html", db, account, lead=get_lead(db, account, lead_id))


@app.post("/follow-ups/{fu_id}/done")
def follow_up_done(fu_id: int, request: Request, db: Session = Depends(get_session),
                   account=Depends(current_account)):
    fu = db.get(FollowUp, fu_id)
    if account is None or fu is None or fu.account_id != account.id:
        raise HTTPException(404, "Follow-up not found")
    fu.done = True
    db.commit()
    lead = fu.lead
    db.refresh(lead)
    return render(request, "_follow_ups.html", db, account, lead=lead)


def search_ctx(db, account):
    return dict(allow=plans.usage(db, account) if account else None,zips=account.zip_list if account else [], industries=account.industry_list if account else [],
                min_confidence=overture.MIN_CONFIDENCE, max_added=overture.MAX_ADDED)


@app.get("/search")
def search_form(request: Request, db: Session = Depends(get_session), account=Depends(current_account)):
    job = search_jobs.latest(db, account.id) if account else None
    done = job if job and job.status != "running" else None
    return render(request, "search.html", db, account, results=done.result_list if done else None, last_job=done,
                  error=done.error if done else None, running=job if job and job.status == "running" else None,
                  **search_ctx(db, account))


@app.post("/search")
def run_search(request: Request, zips: list[str] = Form(default=[]), industries: list[str] = Form(default=[]),
               db: Session = Depends(get_session), account=Depends(current_account)):
    if account is None:
        raise HTTPException(400, "Create an account first")
    error = None
    if not zips or not industries:
        error = "Pick at least one ZIP and one industry."
    elif search_jobs.latest(db, account.id) and search_jobs.latest(db, account.id).status == "running":
        error = "A search is already running. It'll show at the top of the page when it's done."
    if error:
        return render(request, "search.html", db, account, results=None, error=error, running=None,
                      **search_ctx(db, account))
    search_jobs.start(db, account.id, zips, industries)
    return RedirectResponse("/leads?sort=score", status_code=303)


@app.get("/search/status")
def search_status(request: Request, db: Session = Depends(get_session), account=Depends(current_account)):
    """The progress bar, refreshed every few seconds while a search runs."""
    job = search_jobs.for_bar(db, account.id) if account else None
    if job is None:
        return HTMLResponse("")
    return templates.TemplateResponse(request, "_search_bar.html", {"search_job": job})


@app.post("/search/dismiss")
def search_dismiss(next: str = Form(""), db: Session = Depends(get_session), account=Depends(current_account)):
    job = search_jobs.latest(db, account.id) if account else None
    if job and job.status != "running":
        job.seen = True
        db.commit()
    if next.startswith("/"):
        return RedirectResponse(next, status_code=303)
    return HTMLResponse("")


@app.get("/nightly")
def nightly_page(request: Request, db: Session = Depends(get_session), account=Depends(current_account)):
    ctx = {}
    if account:
        st = nightly.get_settings(db, account.id)
        runs = db.scalars(select(NightlyRun).where(NightlyRun.account_id == account.id)
                          .order_by(NightlyRun.started_at.desc()).limit(10)).all()
        ctx = dict(st=st, runs=runs, upcoming=nightly.next_pairs(db, st),
                   cost_each=plans.SCORING_COST_PER_SEARCH, nightly_max=plans.plan_for(db, account)["nightly_max"], has_close=close_crm.enabled_for(db, account), saved=request.query_params.get("saved"))
    return render(request, "nightly.html", db, account, **ctx)


@app.post("/nightly")
def save_nightly(enabled: str = Form(""), run_hour: int = Form(2), searches_per_night: int = Form(20),
                 db: Session = Depends(get_session), account=Depends(current_account)):
    if account is None:
        raise HTTPException(400, "Create an account first")
    st = nightly.get_settings(db, account.id)
    st.enabled = bool(enabled)
    st.run_hour = max(0, min(23, run_hour))
    st.searches_per_night = max(1, min(plans.plan_for(db, account)["nightly_max"], searches_per_night))
    db.commit()
    return RedirectResponse("/nightly?saved=1", status_code=303)


def clean_list(raw: str) -> list[str]:
    """Split text on commas/new lines, trim, drop blanks and repeats, keep order."""
    out = []
    for part in raw.replace("\n", ",").split(","):
        part = " ".join(part.split()).replace(",", "")
        if part and part not in out:
            out.append(part)
    return out


@app.get("/business")
def business_page(request: Request, db: Session = Depends(get_session), account=Depends(current_account)):
    n_emailed = db.scalar(select(func.count()).select_from(Lead).where(
        Lead.account_id == account.id, Lead.deleted_at.is_(None), Lead.last_emailed_at.is_not(None))) if account else 0
    return render(request, "business.html", db, account, categories=catalog.CATEGORIES, all_catalog=catalog.ALL,
                  default_desc=scoring.DEFAULT_DESCRIPTION, n_emailed=n_emailed,
                  gmail_ready=gmail.configured(), can_compose=bool(account and gmail.can_compose(account)),
                  has_email=bool(account and plans.has_email(db, account)), plan=plans.plan_for(db, account) if account else {},
                  close_plan=bool(account and plans.has_close(db, account)), close_on=close_crm.enabled_for(db, account),
                  watched=db.scalars(select(WatchedPage).where(WatchedPage.account_id == account.id).order_by(WatchedPage.id)).all() if account else [],
                  close_shared=bool(account and not account.close_key_enc and close_crm.key_for(db, account)),
                  message=request.query_params.get("msg", ""))


@app.post("/business")
def save_business(description: str = Form(""), zips: str = Form(""), industries: list[str] = Form(default=[]),
                  extra_industries: str = Form(""), watch_pages: str = Form(""),
                  db: Session = Depends(get_session), account=Depends(current_account)):
    if account is None:
        raise HTTPException(400, "Create an account first")
    zip_list = [z for z in clean_list(zips) if z.isdigit() and len(z) == 5]
    inds = clean_list(",".join(industries) + "," + extra_industries)
    if err := plans.territory_error(plans.plan_for(db, account), zip_list, inds):
        return back_to_business(err + " Nothing was saved.")
    account.business_description = description.strip()[:3000]
    account.target_zips, account.industries = ",".join(zip_list), ",".join(inds)
    db.commit()
    local_pages.set_pages(db, account.id, watch_pages.splitlines())
    return RedirectResponse("/business?msg=Saved%20%E2%9C%93", status_code=303)


@app.post("/leads/rescore")
def rescore_leads(db: Session = Depends(get_session), account=Depends(current_account)):
    if account is None:
        raise HTTPException(400, "Create an account first")
    if not ANTHROPIC_API_KEY:
        return back_to_business("Scoring isn't set up on this server.")
    if account.id in scoring._rescoring:
        return back_to_business("Your leads are already being re-scored. Give it a few minutes.")
    n = db.scalar(select(func.count()).select_from(Lead).where(Lead.account_id == account.id, Lead.deleted_at.is_(None)))
    scoring._rescoring.add(account.id)
    search_jobs.spawn(scoring.rescore_all, account.id)
    return back_to_business(f"Re-scoring your {n} leads with your current description. Scores fill in over the next "
                            "few minutes; until then, those leads show no score.")


# ---- Import current clients ----------------------------------------------------------------

def import_target(db: Session, user: User, account: Account | None, account_id: int | None) -> Account:
    """The account to import into: your own, or (for the app owner only) any customer's account."""
    if account_id and (account is None or account_id != account.id):
        if db.get(Organization, user.org_id).plan_tier != "owner":
            raise HTTPException(404)
        target = db.get(Account, account_id)
        if target is None:
            raise HTTPException(404, "Account not found")
        return target
    if account is None:
        raise HTTPException(400, "Create an account first")
    return account


@app.get("/clients/import")
def import_form(request: Request, account_id: int | None = None, db: Session = Depends(get_session),
                user: User = Depends(current_user), account=Depends(current_account)):
    target = import_target(db, user, account, account_id)
    return render(request, "import.html", db, account, target=target, on_behalf=target.id != (account.id if account else None))


@app.post("/clients/import/preview")
async def import_preview(request: Request, file: UploadFile = File(...), account_id: int | None = Form(None),
                         db: Session = Depends(get_session), user: User = Depends(current_user),
                         account=Depends(current_account)):
    import base64
    target = import_target(db, user, account, account_id)
    content = await file.read()
    rows, found, problems = client_import.parse(content[:5_000_000])
    ctx = dict(target=target, on_behalf=target.id != (account.id if account else None), rows=rows[:15], found=found,
               problems=problems, filename=file.filename,
               summary=client_import.preview(db, target.id, rows) if rows else None,
               payload=base64.b64encode(content[:5_000_000]).decode() if rows else "")
    return render(request, "import.html", db, account, **ctx)


@app.post("/clients/import/apply")
def import_apply(payload: str = Form(...), account_id: int | None = Form(None), db: Session = Depends(get_session),
                 user: User = Depends(current_user), account=Depends(current_account)):
    import base64
    from urllib.parse import quote
    target = import_target(db, user, account, account_id)
    rows, _, _ = client_import.parse(base64.b64decode(payload))
    s = client_import.apply(db, target.id, rows)
    msg = (f"Imported {s['marked_sold'] + s['added']} clients: {s['marked_sold']} were already in the leads and are now "
           f"marked Sold, {s['added']} were added as Sold.")
    if target.id != (account.id if account else None):  # the owner importing for a customer: re-score them now
        if target.id not in scoring._rescoring:
            scoring._rescoring.add(target.id)
            search_jobs.spawn(scoring.rescore_all, target.id)
        return RedirectResponse(f"/beta?msg={quote(msg + ' Re-scoring their leads in the background.')}", status_code=303)
    return back_to_business(msg + " Click Re-score all my leads so every lead is judged against your clients.")


def back_to_business(msg: str):
    from urllib.parse import quote
    return RedirectResponse(f"/business?msg={quote(msg)}", status_code=303)


@app.get("/gmail/connect")
def gmail_connect(agree: str = "", db: Session = Depends(get_session), account=Depends(current_account)):
    if account is None or not gmail.configured():
        return back_to_business("Gmail isn't set up yet: the Google sign-in values are missing from .env.")
    if not plans.has_email(db, account):
        return back_to_business(plans.EMAIL_PLAN_MSG)
    if not agree:
        return back_to_business("Tick the box to agree before connecting Gmail.")
    account.gmail_consent_at = datetime.now(timezone.utc)
    db.commit()
    return RedirectResponse(gmail.start_connect(account.id), status_code=303)


@app.get("/gmail/callback")
def gmail_callback(state: str = "", code: str = "", error: str = "", db: Session = Depends(get_session),
                   user: User = Depends(current_user)):
    if error:
        return back_to_business(f"Gmail wasn't connected ({error}). If it says access blocked, your company's Google "
                                "Workspace admin needs to allow this app.")
    try:
        account = gmail.finish_connect(db, state, code)
    except Exception as exc:
        return back_to_business(f"Gmail wasn't connected: {str(exc).splitlines()[0]}")
    resp = back_to_business(f"Connected {account.gmail_address}. Click Check Gmail now to scan your leads.")
    resp.set_cookie(ACCOUNT_COOKIE, str(account.id), httponly=True, samesite="lax", secure=COOKIE_SECURE)
    return resp


@app.post("/gmail/check")
def gmail_check(db: Session = Depends(get_session), account=Depends(current_account)):
    if account is None or not account.gmail_token_enc:
        return back_to_business("Connect Gmail first.")
    if not plans.has_email(db, account):
        return back_to_business(plans.EMAIL_PLAN_MSG)
    try:
        s = gmail.check_account(db, account.id)
    except Exception as exc:
        return back_to_business(f"Gmail check failed: {str(exc).splitlines()[0]}")
    return back_to_business(f"Checked {s['checked']} business websites: you've emailed {s['emailed']} leads. "
                            f"Added {s['follow_ups']} follow-ups (see Due today).")


@app.post("/digest")
def digest_settings(action: str = Form("save"), digest_enabled: str = Form(""),
                    db: Session = Depends(get_session), account=Depends(current_account)):
    if account is None:
        raise HTTPException(400, "Create an account first")
    if action == "send":
        if not plans.has_email(db, account):
            return back_to_business(plans.EMAIL_PLAN_MSG)
        try:
            subject = digest.send(db, account.id)
            return back_to_business(f"Digest sent to {account.gmail_address}: \"{subject}\"")
        except Exception as exc:
            return back_to_business(f"Digest not sent: {str(exc).splitlines()[0]}")
    account.digest_enabled = bool(digest_enabled)
    db.commit()
    return back_to_business("Saved ✓")


@app.post("/gmail/disconnect")
def gmail_disconnect(db: Session = Depends(get_session), account=Depends(current_account)):
    if account:
        gmail.disconnect(db, account)
    return back_to_business("Gmail disconnected. Its stored access was deleted from this app.")


# ---- Setup wizard --------------------------------------------------------------------------

@app.get("/setup")
def setup_page(request: Request, db: Session = Depends(get_session), account=Depends(current_account)):
    if account is None:
        return RedirectResponse("/admin", status_code=303)
    return render(request, "setup.html", db, account, categories=catalog.CATEGORIES, all_catalog=catalog.ALL,
                  selected=account.industry_list, zip_count=len(account.zip_list), default_desc=scoring.DEFAULT_DESCRIPTION)


@app.post("/setup/suggest")
def setup_suggest(request: Request, description: str = Form(""), zips: str = Form(""),
                  db: Session = Depends(get_session), account=Depends(current_account)):
    try:
        selected = setup_mod.suggest_industries(db, account, description) or account.industry_list
        note = "Claude picked these from your description. Change anything you like." if selected else ""
    except Exception as exc:
        selected, note = account.industry_list, f"Couldn't get suggestions ({str(exc).splitlines()[0]}). Pick below."
    zip_count = len([z for z in clean_list(zips) if z.isdigit() and len(z) == 5]) or len(account.zip_list)
    return templates.TemplateResponse(request, "_industry_picker.html", {
        "categories": catalog.CATEGORIES, "all_catalog": catalog.ALL, "selected": selected,
        "zip_count": zip_count, "note": note, "max_pick": plans.plan_for(db, account).get("max_industries")})


@app.get("/setup/estimates")
def setup_estimates(request: Request, zips: str = Query(""), account=Depends(current_account)):
    """How many businesses each type has in these ZIPs (and nearby), for the counts beside the types."""
    zip_list = [z for z in clean_list(zips) if z.isdigit() and len(z) == 5]
    counts = overture.estimates(zip_list, catalog.ALL) if zip_list else {}
    return templates.TemplateResponse(request, "_estimates.html", {
        "counts": counts, "zips": ", ".join(zip_list), "more_zips": len(zip_list) > overture.ESTIMATE_ZIPS,
        "max_zips": overture.ESTIMATE_ZIPS})


@app.post("/setup")
def setup_save(request: Request, description: str = Form(""), zips: str = Form(""), industries: list[str] = Form(default=[]),
               company: str = Form(""), db: Session = Depends(get_session), account=Depends(current_account)):
    from urllib.parse import quote
    user = getattr(request.state, "user", None)
    if company.strip() and user and user.role == "owner" and user.org_id == account.org_id:
        db.get(Organization, account.org_id).name = company.strip()[:200]  # shows on /beta as who is using
    zip_list = [z for z in clean_list(zips) if z.isdigit() and len(z) == 5]
    inds = clean_list(",".join(industries))
    plan = plans.plan_for(db, account)
    account.business_description = description.strip()[:3000]  # kept even if something below is over the limit
    zip_err, ind_err = plans.territory_error(plan, zip_list), plans.territory_error(plan, [], inds)
    if not zip_err:
        account.target_zips = ",".join(zip_list)
    if not ind_err:
        account.industries = ",".join(inds)
    db.commit()
    if zip_err or ind_err:
        return RedirectResponse(f"/setup?msg={quote(zip_err or ind_err)}", status_code=303)
    if not zip_list or not inds:
        return RedirectResponse(f"/setup?msg={quote('Add at least one ZIP code and one industry.')}", status_code=303)
    setup_mod.finish(db, account)
    return RedirectResponse("/setup/first", status_code=303)


@app.get("/setup/first")
def setup_first(request: Request, db: Session = Depends(get_session), account=Depends(current_account)):
    st = nightly.get_settings(db, account.id)
    pairs = len(account.zip_list) * len(account.industry_list)
    n = min(setup_mod.STARTER_SEARCHES, pairs)
    return render(request, "setup_first.html", db, account, n=n, pairs=pairs, st=st, free_shown=plans.FREE_SHOWN,
                  cost=n * plans.SCORING_COST_PER_SEARCH, nights=-(-pairs // max(st.searches_per_night, 1)))


@app.post("/setup/first")
def setup_first_run(db: Session = Depends(get_session), account=Depends(current_account)):
    s = setup_mod.starter_search(db, account)
    search_jobs.reasons_after_free_search(db, account.id)
    msg = (f"Your first search found {s['added']} leads; {s['high']} scored 8+. More arrive every night."
           if not s["error"] else f"First search stopped early: {s['error']}")
    return back_to_leads(msg)


# ---- Billing (Stripe) ------------------------------------------------------------------------

def back_to_billing(msg: str):
    from urllib.parse import quote
    return RedirectResponse(f"/billing?msg={quote(msg)}", status_code=303)


@app.get("/billing")
def billing_page(request: Request, msg: str = "", db: Session = Depends(get_session), user: User = Depends(require_admin)):
    org = db.get(Organization, user.org_id)
    offers = [(t, plans.PLANS[t]) for t in billing.SELLABLE]
    return render(request, "billing.html", db, None, offers=offers, seats=billing.seats(db, org),
                  ready=billing.configured(), message=msg, current=plans.PLANS.get(org.plan_tier, {}),
                  trial_ends=plans.trial_ends(org), now=datetime.now(timezone.utc))


@app.post("/billing/checkout")
def billing_checkout(tier: str = Form(...), db: Session = Depends(get_session), user: User = Depends(require_admin)):
    org = db.get(Organization, user.org_id)
    if not billing.configured():
        return back_to_billing("Billing isn't set up yet.")
    if org.plan_tier == "owner":
        return back_to_billing("The owner account isn't billed.")
    org.checkout_started_at = org.checkout_started_at or datetime.now(timezone.utc)  # for the free search funnel
    db.commit()
    try:
        return RedirectResponse(billing.checkout_url(db, org, tier, user.email), status_code=303)
    except Exception as exc:
        return back_to_billing(f"Couldn't start checkout: {str(exc).splitlines()[0]}")


@app.get("/billing/return")
def billing_return(session_id: str = "", db: Session = Depends(get_session), user: User = Depends(require_admin)):
    try:
        label = billing.finish_checkout(db, db.get(Organization, user.org_id), session_id)
    except Exception as exc:
        return back_to_billing(f"We couldn't confirm the payment: {str(exc).splitlines()[0]}")
    return back_to_billing(f"You're subscribed to the {label} plan. Thank you!")


@app.post("/billing/portal")
def billing_portal(db: Session = Depends(get_session), user: User = Depends(require_admin)):
    org = db.get(Organization, user.org_id)
    if not org.stripe_customer_id:
        return back_to_billing("No subscription yet.")
    return RedirectResponse(billing.portal_url(org), status_code=303)


# ---- Company admin -------------------------------------------------------------------------

def back_to_admin(msg: str):
    from urllib.parse import quote
    return RedirectResponse(f"/admin?msg={quote(msg)}", status_code=303)


@app.get("/admin")
def admin_page(request: Request, msg: str = "", db: Session = Depends(get_session), user: User = Depends(require_admin)):
    from sqlalchemy import case
    from app.models import ApiUsage
    accounts = auth.visible_accounts(db, user)
    ids = [a.id for a in accounts] or [0]
    lead_stats = {r[0]: r[1:] for r in db.execute(
        select(Lead.account_id, func.count(), func.sum(case((Lead.score >= 8, 1), else_=0)),
               func.sum(case((Lead.last_emailed_at.is_not(None), 1), else_=0)),
               func.sum(case((Lead.status == "Sold", 1), else_=0)))
        .where(Lead.account_id.in_(ids), Lead.deleted_at.is_(None)).group_by(Lead.account_id))}
    spend = dict(db.execute(select(ApiUsage.account_id, func.sum(ApiUsage.est_cost_usd))
                            .where(ApiUsage.account_id.in_(ids), ApiUsage.created_at >= places.month_start())
                            .group_by(ApiUsage.account_id)).all())
    searches = dict(db.execute(select(ApiUsage.account_id, func.count())
                               .where(ApiUsage.account_id.in_(ids), ApiUsage.created_at >= places.month_start(),
                                      ApiUsage.service.in_(plans.SEARCH_SERVICES))
                               .group_by(ApiUsage.account_id)).all())
    users = db.scalars(select(User).where(User.org_id == user.org_id).order_by(User.email)).all()
    rows = []
    for a in accounts:
        total, hot, emailed, won = lead_stats.get(a.id, (0, 0, 0, 0))
        rows.append(dict(account=a, users=[u for u in users if u.account_id == a.id], leads=total or 0,
                         hot=hot or 0, emailed=emailed or 0, won=won or 0, spend=float(spend.get(a.id, 0) or 0),
                         searches=searches.get(a.id, 0)))
    return render(request, "admin.html", db, None, rows=rows, users=users, message=msg,
                  totals=dict(leads=sum(r["leads"] for r in rows), hot=sum(r["hot"] for r in rows),
                              won=sum(r["won"] for r in rows), spend=sum(r["spend"] for r in rows),
                              searches=sum(r["searches"] for r in rows)))


@app.post("/admin/publishers")
def admin_add_publisher(name: str = Form(...), email: str = Form(...), zips: str = Form(""),
                        db: Session = Depends(get_session), user: User = Depends(require_admin)):
    email = email.strip().lower()
    if "@" not in email:
        return back_to_admin("Please enter a valid email.")
    if db.scalars(select(User).where(User.email == email)).first():
        return back_to_admin(f"{email} already has an account.")
    zip_list = ",".join(z for z in clean_list(zips) if z.isdigit() and len(z) == 5)
    account = auth.new_account(db, user.org_id, name.strip() or email, zip_list)
    db.add(User(org_id=user.org_id, account_id=account.id, email=email, name=name.strip(), role="publisher"))
    db.commit()
    return back_to_admin(f"Added {email}. They can sign in with Google using that email.")


@app.post("/admin/accounts/{account_id}/zips")
def admin_set_zips(account_id: int, zips: str = Form(""), db: Session = Depends(get_session),
                   user: User = Depends(require_admin)):
    account = db.get(Account, account_id)
    if account is None or account.org_id != user.org_id:
        raise HTTPException(404, "Account not found")
    zip_list = [z for z in clean_list(zips) if z.isdigit() and len(z) == 5]
    if err := plans.territory_error(plans.plan_for(db, account), zip_list):
        return back_to_admin(err)
    account.target_zips = ",".join(zip_list)
    db.commit()
    return back_to_admin(f"Updated ZIP codes for {account.name}.")


@app.post("/admin/users/{user_id}/remove")
def admin_remove_user(user_id: int, db: Session = Depends(get_session), user: User = Depends(require_admin)):
    target = db.get(User, user_id)
    if target is None or target.org_id != user.org_id or target.id == user.id or target.role == "owner":
        return back_to_admin("That person can't be removed.")
    db.delete(target)
    db.commit()
    return back_to_admin(f"Removed {target.email}'s access. Their leads are kept.")


@app.get("/usage")
def usage_page(request: Request, db: Session = Depends(get_session), account=Depends(current_account)):
    from sqlalchemy import func as f
    from app.models import ApiUsage
    ctx = {}
    if account:
        start = places.month_start()
        base = select(ApiUsage).where(ApiUsage.account_id == account.id, ApiUsage.created_at >= start)
        by_service = db.execute(
            select(ApiUsage.service, f.count(), f.sum(ApiUsage.est_cost_usd))
            .where(ApiUsage.account_id == account.id, ApiUsage.created_at >= start)
            .group_by(ApiUsage.service)).all()
        day = f.date(f.timezone("America/New_York", ApiUsage.created_at))
        by_day = db.execute(
            select(day, f.sum(ApiUsage.est_cost_usd))
            .where(ApiUsage.account_id == account.id, ApiUsage.created_at >= start)
            .group_by(day).order_by(day.desc())).all()
        searches_by_day = db.execute(
            select(day, f.count())
            .where(ApiUsage.account_id == account.id, ApiUsage.created_at >= start,
                   ApiUsage.service.in_(plans.SEARCH_SERVICES))
            .group_by(day).order_by(day.desc())).all()
        ctx = dict(by_service=by_service, by_day=by_day, searches_by_day=searches_by_day,
                   spent=places.spent_this_month(db, account.id), limit=plans.monthly_limit(db, account),
                   allow=plans.usage(db, account),
                   overall=places.spent_this_month(db),
                   month=start.strftime("%B %Y"))
    return render(request, "usage.html", db, account, **ctx)


# ---- Dashboard -----------------------------------------------------------------------------

@app.get("/dashboard")
def dashboard_page(request: Request, status: str = "", db: Session = Depends(get_session), account=Depends(current_account)):
    rows = dashboard.counts(db, account.id) if account else []
    chosen = status if status in STATUSES else ""
    ctx = dict(rows=rows, total=sum(r["count"] for r in rows), size=dashboard.SIZE, chosen=chosen,
               missing=dashboard.sales_missing_amounts(db, account.id) if account else 0,
               reasons=signals.active(db, account.id, limit=25) if account else [],
               like_customers=dashboard.like_your_customers(db, account.id) if account else [],
               checking_news=bool(account and account.id in _checking_news), message=request.query_params.get("msg", ""))
    if chosen:
        ctx.update(breakdown_ctx(db, account, chosen))
    if locked := plans.locked_ids(db, account):  # a Free search's locked leads stay hidden here too
        ctx["reasons"] = [r for r in ctx["reasons"] if r.lead_id not in locked]
        for group in ctx["like_customers"]:
            group["top"] = [l for l in group["top"] if l.id not in locked]
    return render(request, "dashboard.html", db, account, **ctx)


_checking_news: set[int] = set()  # accounts whose websites are being checked right now


@app.post("/signals/check")
def signals_check_now(db: Session = Depends(get_session), account=Depends(current_account)):
    """Look for reasons to reach out now (in the background) instead of waiting for tonight."""
    if account and account.id not in _checking_news:
        _checking_news.add(account.id)

        def run(account_id: int):
            from app.db import engine as eng
            try:
                with Session(eng) as s:
                    signals.check_account(s, account_id)
            finally:
                _checking_news.discard(account_id)
        search_jobs.spawn(run, account.id)
    from urllib.parse import quote
    return RedirectResponse(f"/dashboard?msg={quote('Checking your top leads’ websites for news. New reasons show up here over the next few minutes.')}",
                            status_code=303)


@app.post("/local-pages/check")
def local_pages_check_now(db: Session = Depends(get_session), account=Depends(current_account)):
    """Read the watched local pages now (in the background) instead of waiting for the weekly check."""
    if account and account.id not in _checking_news:
        _checking_news.add(account.id)

        def run(account_id: int):
            from app.db import engine as eng
            try:
                with Session(eng) as s:
                    for page in s.scalars(select(WatchedPage).where(WatchedPage.account_id == account_id)):
                        page.last_checked_at = None
                    s.commit()
                    local_pages.check_account(s, account_id)
                    site_check.check_unchecked(s, account_id, limit=200)
                    if ANTHROPIC_API_KEY:
                        scoring.score_unscored(s, account_id, limit=200)
            finally:
                _checking_news.discard(account_id)
        search_jobs.spawn(run, account.id)
    return back_to_business("Reading your local pages now. Businesses they mention show up on your Dashboard "
                            "under Reasons to reach out within a few minutes.")


@app.post("/signals/{signal_id}/done")
def signal_done(signal_id: int, db: Session = Depends(get_session), account=Depends(current_account)):
    sig = db.get(LeadSignal, signal_id)
    if account is None or sig is None or sig.account_id != account.id:
        raise HTTPException(404, "Not found")
    sig.done_at = datetime.now(timezone.utc)
    db.commit()
    return HTMLResponse("")


@app.get("/dashboard/breakdown")
def dashboard_breakdown(request: Request, status: str = "", db: Session = Depends(get_session), account=Depends(current_account)):
    if account is None or status not in STATUSES:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "_breakdown.html", breakdown_ctx(db, account, status))


def breakdown_ctx(db: Session, account: Account, status: str) -> dict:
    leads, total = dashboard.leads_in(db, account.id, status)
    if locked := plans.locked_ids(db, account):
        leads = [l for l in leads if l.id not in locked]
        total = db.scalar(select(func.count()).select_from(Lead).where(
            Lead.account_id == account.id, Lead.deleted_at.is_(None), Lead.status == status, Lead.id.not_in(locked)))
    return dict(status=status, label=dashboard.LABELS.get(status, status), color=dashboard.COLORS[status],
                b_leads=leads, b_total=total)


# ---- Results -------------------------------------------------------------------------------

@app.get("/results")
def my_results(request: Request, db: Session = Depends(get_session), account=Depends(current_account)):
    ctx = {}
    if account:
        ctx = dict(all_time=results.metrics(db, [account.id])[account.id],
                   month=results.metrics(db, [account.id], since=places.month_start())[account.id],
                   month_name=places.month_start().strftime("%B"), started=account.created_at)
    return render(request, "results.html", db, account, **ctx)


def beta_rows(db: Session) -> list[dict]:
    """One row per customer company (not the owner's), with its results added up."""
    orgs = db.scalars(select(Organization).where(Organization.plan_tier != "owner").order_by(Organization.created_at)).all()
    accounts = db.scalars(select(Account).where(Account.org_id.in_([o.id for o in orgs] or [0]))).all()
    per_account = results.metrics(db, [a.id for a in accounts])
    users = db.scalars(select(User).where(User.org_id.in_([o.id for o in orgs] or [0]))).all()
    notes = dict(db.execute(select(Feedback.account_id, func.count()).group_by(Feedback.account_id)).all())
    surveys = {}
    for s in db.scalars(select(BetaSurvey).order_by(BetaSurvey.created_at)):  # latest answer per account wins
        surveys[s.account_id] = s
    rows = []
    for o in orgs:
        mine = [a for a in accounts if a.org_id == o.id]
        people = [u for u in users if u.org_id == o.id]
        if not people:  # demo companies nobody signs in to aren't testers
            continue
        if o.signup_offer == "free" and o.plan != "active":  # free search sign-ups have their own table until they pay
            continue
        logins = [u.last_login_at for u in people if u.last_login_at]
        rows.append(dict(org=o, accounts=mine, plan=plans.PLANS.get(o.plan_tier, {}).get("label", o.plan_tier),
                         emails=", ".join(u.email for u in people),
                         setup_done=all(a.setup_done_at for a in mine) if mine else False,
                         last_login=max(logins) if logins else None,
                         feedback=sum(notes.get(a.id, 0) for a in mine),
                         survey=next((surveys[a.id] for a in mine if a.id in surveys), None),
                         **activity(people), **results.total([per_account[a.id] for a in mine])))
    return rows


def free_search_rows(db: Session) -> list[dict]:
    """Everyone who signed up from the /try link, newest first, with how far they got."""
    orgs = db.scalars(select(Organization).where(Organization.signup_offer == "free")
                      .order_by(Organization.created_at.desc())).all()
    rows = []
    for o in orgs:
        acct = db.scalars(select(Account).where(Account.org_id == o.id).order_by(Account.id)).first()
        people = db.scalars(select(User).where(User.org_id == o.id)).all()
        rows.append(dict(
            org=o, emails=", ".join(u.email for u in people),
            search=f"{', '.join(acct.industry_list)} in {', '.join(acct.zip_list)}" if acct and acct.setup_done_at else "",
            leads_at=plans.first_leads_at(db, acct.id) if acct else None,
            leads=db.scalar(select(func.count()).select_from(Lead).where(Lead.account_id == acct.id)) if acct else 0,
            subscribed=o.plan == "active", **activity(people)))
    return rows


def survey_cells(s: BetaSurvey | None) -> list:
    if s is None:
        return ["", "", "", "", ""]
    return [{**WOULD_PAY_OLD, **WOULD_PAY}.get(s.would_pay, ""), "" if s.recommend is None else s.recommend,
            "" if s.hours_saved is None else float(s.hours_saved), s.quote, "yes" if s.quote_ok else "no"]


def require_owner(db: Session, user: User) -> None:
    if db.get(Organization, user.org_id).plan_tier != "owner":
        raise HTTPException(404)


@app.get("/beta")
def beta_results(request: Request, db: Session = Depends(get_session), user: User = Depends(current_user)):
    require_owner(db, user)
    rows = beta_rows(db)
    message = request.query_params.get("msg", "")
    answered = [r["survey"] for r in rows if r["survey"]]
    scores = [s.recommend for s in answered if s.recommend is not None]
    hours = [float(s.hours_saved) for s in answered if s.hours_saved is not None]
    survey = dict(answered=len(answered), avg_recommend=round(sum(scores) / len(scores), 1) if scores else None,
                  would_pay=sum(1 for s in answered if s.would_pay in ("personal", "pro", "premium", "yes")),
                  avg_hours=round(sum(hours) / len(hours), 1) if hours else None,
                  quotes=[(r["org"].name, r["survey"]) for r in rows if r["survey"] and r["survey"].quote])
    free = free_search_rows(db)
    funnel = [("Signed up", len(free)), ("Ran their search", sum(1 for r in free if r["leads_at"])),
              ("Saw the $49 offer", sum(1 for r in free if r["org"].offer_seen_at)),
              ("Clicked Subscribe", sum(1 for r in free if r["org"].checkout_started_at)),
              ("Subscribed", sum(1 for r in free if r["subscribed"])),
              ("Interested in a VA", sum(1 for r in free if r["org"].va_interest_at))]
    return render(request, "beta.html", db, None, rows=rows, totals=results.total(rows) if rows else None, survey=survey,
                  message=message, free=free, funnel=funnel)


@app.get("/beta.csv")
def beta_results_csv(db: Session = Depends(get_session), user: User = Depends(current_user)):
    import csv
    import io
    require_owner(db, user)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Company", "People", "Plan", "Signed up", "Setup done", "Last sign-in", "Minutes in app", "Visits",
                "Last seen", "Agreed to terms", "Leads contacted",
                "Appointments set", "Appointments shown", "Sold", "Monthly recurring revenue $", "Contract value $",
                "Ad Management Fees $", "Sales found only with Plug Employ", "Feedback notes", "Would pay",
                "Recommend (0-10)", "Hours saved per week", "Quote", "OK to quote"])
    for r in beta_rows(db):
        w.writerow([r["org"].name, r["emails"], r["plan"], r["org"].created_at.date(), "yes" if r["setup_done"] else "no",
                    r["last_login"].date() if r["last_login"] else "", round(r["active_seconds"] / 60), r["visits"],
                    r["last_seen"].date() if r["last_seen"] else "",
                    r["agreed"].date() if r["agreed"] else "", r["contacted"], r["appts_set"],
                    r["appts_shown"], r["sold"], round(r["mrr"]), round(r["contract_value"]), round(r["ad_fees"]),
                    r["found_only"], r["feedback"]] + survey_cells(r["survey"]))
    # Free search sign-ups (from the /try link) below the testers, in their own section.
    def day(when):
        return when.astimezone().date() if when else ""
    w.writerow([])
    w.writerow(["FREE SEARCH SIGN-UPS"])
    w.writerow(["Company", "People", "Signed up", "Their search", "Leads", "Minutes in app", "Visits", "Last seen",
                "Agreed to terms", "Saw $49 offer", "Clicked Subscribe", "Subscribed", "Interested in a VA"])
    for r in free_search_rows(db):
        o = r["org"]
        w.writerow([csv_cell(o.name), r["emails"], day(o.created_at), csv_cell(r["search"]) or "setup not finished",
                    r["leads"] if r["leads_at"] else "", round(r["active_seconds"] / 60), r["visits"], day(r["last_seen"]),
                    day(r["agreed"]), day(o.offer_seen_at), day(o.checkout_started_at), "yes" if r["subscribed"] else "",
                    day(o.va_interest_at)])
    return HTMLResponse(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="beta-results-{date.today()}.csv"'})


# ---- End-of-beta survey ---------------------------------------------------------------------

@app.get("/survey")
def survey_page(request: Request, db: Session = Depends(get_session), account=Depends(current_account)):
    """The survey as a page (the pop-up shows the same form). Handy for previewing it."""
    return render(request, "survey.html", db, account)


@app.post("/survey")
def survey_save(would_pay: str = Form(""), recommend: str = Form(""), hours_saved: str = Form(""), quote: str = Form(""),
                quote_ok: str = Form(""), next: str = Form("/leads"), db: Session = Depends(get_session),
                user: User = Depends(current_user), account=Depends(current_account)):
    if account is None:
        raise HTTPException(400, "Create an account first")
    score = money(recommend, None)
    db.add(BetaSurvey(account_id=account.id, user_id=user.id, would_pay=would_pay if would_pay in WOULD_PAY else "",
                      recommend=min(10, int(score)) if score is not None else None,
                      hours_saved=money(hours_saved, None), quote=quote.strip()[:2000], quote_ok=bool(quote_ok)))
    db.commit()
    from urllib.parse import quote as q
    target = next if next.startswith("/") else "/leads"
    return RedirectResponse(f"{target}{'&' if '?' in target else '?'}msg={q('Thank you for the feedback!')}", status_code=303)


@app.post("/survey/later")
def survey_later():
    resp = HTMLResponse("")
    resp.set_cookie(SURVEY_LATER_COOKIE, "1", max_age=3 * 86400, httponly=True, samesite="lax", secure=COOKIE_SECURE)
    return resp


# ---- Feedback from testers ----------------------------------------------------------------

@app.post("/feedback")
def send_feedback(text: str = Form(""), page: str = Form(""), db: Session = Depends(get_session),
                  user: User = Depends(current_user), account=Depends(current_account)):
    if not text.strip():
        return HTMLResponse('<p class="text-xs text-red-700">Type something first.</p>')
    db.add(Feedback(account_id=account.id if account else None, user_id=user.id, page=page[:300], text=text.strip()[:5000]))
    db.commit()
    return HTMLResponse('<p class="text-sm text-green-700">Thanks! Savannah will see this.</p>')


@app.get("/feedback")
def feedback_list(request: Request, db: Session = Depends(get_session), user: User = Depends(current_user)):
    org = db.get(Organization, user.org_id)
    if org.plan_tier != "owner":  # only the app owner reads everyone's feedback
        raise HTTPException(404)
    rows = db.execute(select(Feedback, User.email, Organization.name).outerjoin(User, User.id == Feedback.user_id)
                      .outerjoin(Organization, Organization.id == User.org_id)
                      .order_by(Feedback.created_at.desc()).limit(500)).all()
    return render(request, "feedback.html", db, None, rows=rows)


@app.get("/status")
def status_page(request: Request, user: User = Depends(current_user)):
    return templates.TemplateResponse(request, "index.html", {"db": check_database()})


@app.get("/db-status")
def db_status(request: Request, user: User = Depends(current_user)):
    """Just the database box, so the "Check again" button can refresh it (HTMX)."""
    return templates.TemplateResponse(request, "_db_status.html", {"db": check_database()})


@app.get("/health")
def health():
    db = check_database()
    return {"app": "ok", "database": "ok" if db["ok"] else "error"}

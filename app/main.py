"""Plug Employ Intelligence: lead finding, scoring and tracking for local ad sellers."""

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db import check_database, get_session
import anthropic
import httpx

from app import auth, billing, catalog, close_crm, plans, digest, gmail, nightly, outreach, places, scoring
from app.auth import ACCOUNT_COOKIE, current_account, current_user, require_admin
from app import setup as setup_mod
from app import website as site_check
from app.config import ANTHROPIC_API_KEY, GOOGLE_PLACES_API_KEY, MONTHLY_API_BUDGET_USD
from app.models import OUTCOMES, STATUSES, Account, FollowUp, Lead, LeadNote, NightlyRun, Organization, User

INDUSTRIES = places.INDUSTRIES

@asynccontextmanager
async def lifespan(app):
    sched = nightly.start_scheduler()  # overnight lead search
    yield
    sched.shutdown(wait=False)


app = FastAPI(title="Plug Employ Intelligence", lifespan=lifespan)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")



@app.exception_handler(auth.LoginRequired)
def login_required(request: Request, exc: auth.LoginRequired):
    if request.headers.get("HX-Request"):
        return HTMLResponse("", headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


def render(request, name, db, account, **ctx):
    user = getattr(request.state, "user", None)
    accounts = auth.visible_accounts(db, user) if user else []
    org = db.get(Organization, user.org_id) if user else None
    return templates.TemplateResponse(
        request, name,
        {"account": account, "accounts": accounts, "user": user, "org": org, "statuses": STATUSES,
         "today": date.today(), **ctx},
    )


def get_lead(db: Session, account: Account | None, lead_id: int) -> Lead:
    """Load a lead only if it belongs to the current account."""
    lead = db.get(Lead, lead_id)
    if account is None or lead is None or lead.account_id != account.id:
        raise HTTPException(404, "Lead not found")
    return lead


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


@app.get("/auth/google")
def auth_google(company: str = ""):
    if not gmail.configured():
        return RedirectResponse("/login?msg=Google%20sign-in%20isn%27t%20set%20up%20yet.", status_code=303)
    return RedirectResponse(auth.start_login(company), status_code=303)


@app.get("/auth/callback")
def auth_callback(state: str = "", code: str = "", error: str = "", db: Session = Depends(get_session)):
    from urllib.parse import quote
    if error:
        return RedirectResponse(f"/login?msg={quote('Sign-in cancelled.')}", status_code=303)
    try:
        identity, company = auth.google_identity(state, code)
        user = auth.sign_in(db, identity["email"], identity["name"], company)
    except Exception as exc:
        return RedirectResponse(f"/login?msg={quote(str(exc).splitlines()[0])}", status_code=303)
    resp = RedirectResponse("/setup" if company else "/leads", status_code=303)
    resp.set_cookie(auth.SESSION_COOKIE, auth.make_session(user.id), httponly=True, samesite="lax",
                    max_age=auth.SESSION_DAYS * 86400)
    resp.delete_cookie(ACCOUNT_COOKIE)
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
    resp.set_cookie(ACCOUNT_COOKIE, str(account_id), httponly=True, samesite="lax")
    return resp


@app.get("/leads")
def leads_list(request: Request, status: str = "", zip_code: str = "", industry: str = "", sort: str = "follow_up", new: str = "",
               hide_contacted: str = "",
               db: Session = Depends(get_session), account=Depends(current_account)):
    if account and account.setup_done_at is None:
        return RedirectResponse("/setup", status_code=303)
    leads, last_run = [], None
    if account:
        last_run = db.scalars(select(NightlyRun).where(NightlyRun.account_id == account.id)
                              .order_by(NightlyRun.started_at.desc())).first()
        q = (select(Lead).where(Lead.account_id == account.id)
             .options(selectinload(Lead.notes), selectinload(Lead.follow_ups)))
        if status:
            q = q.where(Lead.status == status)
        if zip_code:
            q = q.where(Lead.zip_code == zip_code)
        if industry:
            q = q.where(Lead.industry == industry)
        if hide_contacted:
            q = q.where(Lead.last_emailed_at.is_(None))
        if new and last_run:
            q = q.where(Lead.created_at >= last_run.started_at)
        leads = list(db.scalars(q.order_by(Lead.business_name)))
        if sort == "score":
            leads.sort(key=lambda l: -(l.score or 0))
        elif sort == "industry":
            leads.sort(key=lambda l: ((l.industry or "~").lower(), -(l.score or 0)))
        elif sort == "follow_up":
            leads.sort(key=lambda l: l.next_follow_up.due_date if l.next_follow_up else date.max)
    industries = sorted({i for i in db.scalars(select(Lead.industry).where(
        Lead.account_id == (account.id if account else 0))) if i}, key=str.lower)
    zips = sorted({z for z in db.scalars(select(Lead.zip_code).where(Lead.account_id == (account.id if account else 0)))})
    unscored = sum(1 for l in leads if l.score is None)
    return render(request, "leads.html", db, account, leads=leads, zips=zips, industries=industries,
                  f_status=status, f_zip=zip_code, f_industry=industry, sort=sort, unscored=unscored, f_new=new, f_hide=hide_contacted, last_run=last_run,
                  working=scoring.whats_working(db, account.id) if account else {},
                  allow=plans.usage(db, account) if account else None,
                  has_claude_key=bool(ANTHROPIC_API_KEY), has_close=close_crm.enabled_for(db, account), can_draft=bool(account and gmail.can_compose(account)), message=request.query_params.get("msg", ""))


@app.post("/leads/score")
def score_leads(db: Session = Depends(get_session), account=Depends(current_account)):
    if account is None:
        raise HTTPException(400, "Create an account first")
    if not ANTHROPIC_API_KEY:
        msg = "No Claude key in .env yet."
    else:
        try:
            s = scoring.score_unscored(db, account.id)
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
            select(FollowUp).where(FollowUp.account_id == account.id, FollowUp.done.is_(False),
                                   FollowUp.due_date <= date.today())
            .options(selectinload(FollowUp.lead)).order_by(FollowUp.due_date)
        ).all()
    return render(request, "due.html", db, account, items=items)


@app.get("/leads/new")
def new_lead_form(request: Request, db: Session = Depends(get_session), account=Depends(current_account)):
    return render(request, "lead_new.html", db, account)


@app.post("/leads/new")
def create_lead(business_name: str = Form(...), address: str = Form(""), zip_code: str = Form(""),
                phone: str = Form(""), website: str = Form(""), industry: str = Form(""),
                db: Session = Depends(get_session), account=Depends(current_account)):
    if account is None:
        raise HTTPException(400, "Create an account first")
    lead = Lead(account_id=account.id, business_name=business_name.strip(), address=address.strip(),
                zip_code=zip_code.strip(), phone=phone.strip(), website=website.strip(),
                industry=industry.strip(), status="New")
    db.add(lead)
    db.commit()
    return RedirectResponse(f"/leads/{lead.id}", status_code=303)


@app.get("/leads/{lead_id}")
def lead_detail(lead_id: int, request: Request, db: Session = Depends(get_session),
                account=Depends(current_account)):
    return render(request, "lead_detail.html", db, account, lead=get_lead(db, account, lead_id),
                  can_draft=bool(account and gmail.can_compose(account)), has_close=close_crm.enabled_for(db, account))


@app.post("/leads/{lead_id}/status")
def set_status(lead_id: int, request: Request, status: str = Form(...), reason: str = Form(""),
               contract_end: str = Form(""), db: Session = Depends(get_session), account=Depends(current_account)):
    lead = get_lead(db, account, lead_id)
    if status not in STATUSES:
        raise HTTPException(400, "Unknown status")
    if status in OUTCOMES:
        if lead.status != status:
            lead.outcome_at = datetime.now(timezone.utc)
        lead.outcome_reason = reason.strip()[:500]
    else:
        lead.outcome_reason, lead.outcome_at = "", None
    lead.status = status
    if status == "Won":
        set_renewal(db, lead, date.fromisoformat(contract_end) if contract_end else None)
    db.commit()
    return render(request, "_status.html", db, account, lead=lead, saved=True)


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
    if not gmail.can_compose(account):
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
def close_send_top(db: Session = Depends(get_session), account=Depends(current_account)):
    if not close_crm.enabled_for(db, account):
        return back_to_leads("Close isn't connected for this account.")
    try:
        s = close_crm.send_leads(db, close_crm.top_unsent(db, account.id))
    except Exception as exc:
        return back_to_leads(f"Close error: {str(exc).splitlines()[0]}")
    return back_to_leads(f"Sent {s['sent']} leads to Close as Potential. {s['already_in_close']} were already in Close."
                         + (f" {s['failed']} failed." if s["failed"] else ""))


@app.post("/close/sync")
def close_sync(db: Session = Depends(get_session), account=Depends(current_account)):
    if not close_crm.enabled_for(db, account):
        return back_to_leads("Close isn't connected for this account.")
    try:
        s = close_crm.sync_back(db, account.id)
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
        s = close_crm.send_leads(db, [lead])
    except Exception as exc:
        return HTMLResponse(f'<span class="text-sm text-red-700">{escape(str(exc).splitlines()[0])}</span>')
    msg = "Sent to Close as Potential ✓" if s["sent"] else "Already in Close ✓" if s["already_in_close"] else "Couldn't send."
    return HTMLResponse(f'<span class="text-sm text-green-700">{msg}</span>')


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
    return dict(allow=plans.usage(db, account) if account else None,zips=account.zip_list if account else [], industries=account.industry_list if account else [], spent=places.spent_this_month(db),
                budget=MONTHLY_API_BUDGET_USD, cost_each=places.COST_PER_REQUEST_USD,
                min_reviews=places.MIN_REVIEWS, has_key=bool(GOOGLE_PLACES_API_KEY))


@app.get("/search")
def search_form(request: Request, db: Session = Depends(get_session), account=Depends(current_account)):
    return render(request, "search.html", db, account, results=None, error=None, **search_ctx(db, account))


@app.post("/search")
def run_search(request: Request, zips: list[str] = Form(default=[]), industries: list[str] = Form(default=[]),
               db: Session = Depends(get_session), account=Depends(current_account)):
    results, error = [], None
    if account is None:
        raise HTTPException(400, "Create an account first")
    if not GOOGLE_PLACES_API_KEY:
        error = "No Google key in .env yet."
    elif not zips or not industries:
        error = "Pick at least one ZIP and one industry."
    else:
        try:
            recent = places.recently_searched(db, account.id)
            for z in zips:
                for ind in industries:
                    if f"{ind} in {z}" in recent:
                        results.append({"zip": z, "industry": ind, "recent": True})
                        continue
                    results.append(places.search_and_save(db, account.id, z, ind))
            if account.gmail_token_enc:
                gmail.check_account(db, account.id)
            site_check.check_unchecked(db, account.id)
            if ANTHROPIC_API_KEY:
                scoring.score_unscored(db, account.id)
        except places.BudgetExceeded as exc:
            error = str(exc) + " Search stopped."
        except anthropic.APIError as exc:
            error = f"Claude returned an error: {exc}"
        except httpx.HTTPError as exc:
            error = f"Google returned an error: {str(exc).splitlines()[0]}"
    return render(request, "search.html", db, account, results=results, error=error, **search_ctx(db, account))


@app.get("/nightly")
def nightly_page(request: Request, db: Session = Depends(get_session), account=Depends(current_account)):
    ctx = {}
    if account:
        st = nightly.get_settings(db, account.id)
        runs = db.scalars(select(NightlyRun).where(NightlyRun.account_id == account.id)
                          .order_by(NightlyRun.started_at.desc()).limit(10)).all()
        ctx = dict(st=st, runs=runs, upcoming=nightly.next_pairs(db, st),
                   cost_each=places.COST_PER_REQUEST_USD, has_close=close_crm.enabled_for(db, account), saved=request.query_params.get("saved"))
    return render(request, "nightly.html", db, account, **ctx)


@app.post("/nightly")
def save_nightly(enabled: str = Form(""), run_hour: int = Form(2), searches_per_night: int = Form(20),
                 db: Session = Depends(get_session), account=Depends(current_account)):
    if account is None:
        raise HTTPException(400, "Create an account first")
    st = nightly.get_settings(db, account.id)
    st.enabled = bool(enabled)
    st.run_hour = max(0, min(23, run_hour))
    st.searches_per_night = max(1, min(100, searches_per_night))
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
        Lead.account_id == account.id, Lead.last_emailed_at.is_not(None))) if account else 0
    return render(request, "business.html", db, account, categories=catalog.CATEGORIES, all_catalog=catalog.ALL,
                  default_desc=scoring.DEFAULT_DESCRIPTION, n_emailed=n_emailed,
                  gmail_ready=gmail.configured(), can_compose=bool(account and gmail.can_compose(account)), message=request.query_params.get("msg", ""))


@app.post("/business")
def save_business(description: str = Form(""), zips: str = Form(""), industries: list[str] = Form(default=[]),
                  extra_industries: str = Form(""), db: Session = Depends(get_session), account=Depends(current_account)):
    if account is None:
        raise HTTPException(400, "Create an account first")
    account.business_description = description.strip()[:3000]
    account.target_zips = ",".join(z for z in clean_list(zips) if z.isdigit() and len(z) == 5)
    account.industries = ",".join(clean_list(",".join(industries) + "," + extra_industries))
    db.commit()
    return RedirectResponse("/business?msg=Saved%20%E2%9C%93", status_code=303)


def back_to_business(msg: str):
    from urllib.parse import quote
    return RedirectResponse(f"/business?msg={quote(msg)}", status_code=303)


@app.get("/gmail/connect")
def gmail_connect(account=Depends(current_account)):
    if account is None or not gmail.configured():
        return back_to_business("Gmail isn't set up yet: the Google sign-in values are missing from .env.")
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
    resp.set_cookie(ACCOUNT_COOKIE, str(account.id), httponly=True, samesite="lax")
    return resp


@app.post("/gmail/check")
def gmail_check(db: Session = Depends(get_session), account=Depends(current_account)):
    if account is None or not account.gmail_token_enc:
        return back_to_business("Connect Gmail first.")
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
        "zip_count": zip_count, "note": note})


@app.post("/setup")
def setup_save(description: str = Form(""), zips: str = Form(""), industries: list[str] = Form(default=[]),
               db: Session = Depends(get_session), account=Depends(current_account)):
    from urllib.parse import quote
    zip_list = [z for z in clean_list(zips) if z.isdigit() and len(z) == 5]
    inds = clean_list(",".join(industries))
    account.business_description = description.strip()[:3000]
    account.target_zips, account.industries = ",".join(zip_list), ",".join(inds)
    db.commit()
    if not zip_list or not inds:
        return RedirectResponse(f"/setup?msg={quote('Add at least one ZIP code and one industry.')}", status_code=303)
    setup_mod.finish(db, account)
    return RedirectResponse("/setup/first", status_code=303)


@app.get("/setup/first")
def setup_first(request: Request, db: Session = Depends(get_session), account=Depends(current_account)):
    st = nightly.get_settings(db, account.id)
    pairs = len(account.zip_list) * len(account.industry_list)
    n = min(setup_mod.STARTER_SEARCHES, pairs)
    return render(request, "setup_first.html", db, account, n=n, pairs=pairs, st=st,
                  cost=n * places.COST_PER_REQUEST_USD, nights=-(-pairs // max(st.searches_per_night, 1)))


@app.post("/setup/first")
def setup_first_run(db: Session = Depends(get_session), account=Depends(current_account)):
    s = setup_mod.starter_search(db, account)
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
                  ready=billing.configured(), message=msg, current=plans.PLANS.get(org.plan_tier, {}))


@app.post("/billing/checkout")
def billing_checkout(tier: str = Form(...), db: Session = Depends(get_session), user: User = Depends(require_admin)):
    org = db.get(Organization, user.org_id)
    if not billing.configured():
        return back_to_billing("Billing isn't set up yet.")
    if org.plan_tier == "owner":
        return back_to_billing("The owner account isn't billed.")
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
               func.sum(case((Lead.status == "Won", 1), else_=0)))
        .where(Lead.account_id.in_(ids)).group_by(Lead.account_id))}
    spend = dict(db.execute(select(ApiUsage.account_id, func.sum(ApiUsage.est_cost_usd))
                            .where(ApiUsage.account_id.in_(ids), ApiUsage.created_at >= places.month_start())
                            .group_by(ApiUsage.account_id)).all())
    users = db.scalars(select(User).where(User.org_id == user.org_id).order_by(User.email)).all()
    rows = []
    for a in accounts:
        total, hot, emailed, won = lead_stats.get(a.id, (0, 0, 0, 0))
        rows.append(dict(account=a, users=[u for u in users if u.account_id == a.id], leads=total or 0,
                         hot=hot or 0, emailed=emailed or 0, won=won or 0, spend=float(spend.get(a.id, 0) or 0)))
    return render(request, "admin.html", db, None, rows=rows, users=users, message=msg,
                  totals=dict(leads=sum(r["leads"] for r in rows), hot=sum(r["hot"] for r in rows),
                              won=sum(r["won"] for r in rows), spend=sum(r["spend"] for r in rows)))


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
    account.target_zips = ",".join(z for z in clean_list(zips) if z.isdigit() and len(z) == 5)
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
        ctx = dict(by_service=by_service, by_day=by_day,
                   spent=places.spent_this_month(db, account.id), limit=plans.monthly_limit(db, account),
                   allow=plans.usage(db, account),
                   overall=places.spent_this_month(db), overall_cap=MONTHLY_API_BUDGET_USD,
                   month=start.strftime("%B %Y"))
    return render(request, "usage.html", db, account, **ctx)


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

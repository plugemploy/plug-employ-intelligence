"""Plug Employ Intelligence: accounts and lead tracker."""

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db import check_database, get_session
import anthropic
import httpx

from app import close_crm, digest, gmail, nightly, outreach, places, scoring
from app import website as site_check
from app.config import ANTHROPIC_API_KEY, GOOGLE_PLACES_API_KEY, MONTHLY_API_BUDGET_USD
from app.models import OUTCOMES, STATUSES, Account, FollowUp, Lead, LeadNote, NightlyRun

INDUSTRIES = places.INDUSTRIES

@asynccontextmanager
async def lifespan(app):
    sched = nightly.start_scheduler()  # overnight lead search
    yield
    sched.shutdown(wait=False)


app = FastAPI(title="Plug Employ Intelligence", lifespan=lifespan)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Prototype: the account is picked from a dropdown and kept in a cookie.
# Real logins replace this in a later phase.
ACCOUNT_COOKIE = "account_id"


def current_account(request: Request, db: Session = Depends(get_session)) -> Account | None:
    raw = request.cookies.get(ACCOUNT_COOKIE, "")
    account = db.get(Account, int(raw)) if raw.isdigit() else None
    return account or db.scalars(select(Account).order_by(Account.id)).first()


def render(request, name, db, account, **ctx):
    accounts = db.scalars(select(Account).order_by(Account.name)).all()
    return templates.TemplateResponse(
        request, name,
        {"account": account, "accounts": accounts, "statuses": STATUSES, "today": date.today(), **ctx},
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


@app.post("/switch-account")
def switch_account(account_id: int = Form(...)):
    resp = RedirectResponse("/leads", status_code=303)
    resp.set_cookie(ACCOUNT_COOKIE, str(account_id), httponly=True, samesite="lax")
    return resp


@app.get("/leads")
def leads_list(request: Request, status: str = "", zip_code: str = "", sort: str = "follow_up", new: str = "",
               hide_contacted: str = "",
               db: Session = Depends(get_session), account=Depends(current_account)):
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
        if hide_contacted:
            q = q.where(Lead.last_emailed_at.is_(None))
        if new and last_run:
            q = q.where(Lead.created_at >= last_run.started_at)
        leads = list(db.scalars(q.order_by(Lead.business_name)))
        if sort == "score":
            leads.sort(key=lambda l: -(l.score or 0))
        elif sort == "follow_up":
            leads.sort(key=lambda l: l.next_follow_up.due_date if l.next_follow_up else date.max)
    zips = sorted({z for z in db.scalars(select(Lead.zip_code).where(Lead.account_id == (account.id if account else 0)))})
    unscored = sum(1 for l in leads if l.score is None)
    return render(request, "leads.html", db, account, leads=leads, zips=zips,
                  f_status=status, f_zip=zip_code, sort=sort, unscored=unscored, f_new=new, f_hide=hide_contacted, last_run=last_run,
                  working=scoring.whats_working(db, account.id) if account else {},
                  has_claude_key=bool(ANTHROPIC_API_KEY), has_close=close_crm.configured(), can_draft=bool(account and gmail.can_compose(account)), message=request.query_params.get("msg", ""))


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
                  can_draft=bool(account and gmail.can_compose(account)), has_close=close_crm.configured())


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
    if account is None or not close_crm.configured():
        return back_to_leads("Close isn't connected: add CLOSE_API_KEY to .env.")
    try:
        s = close_crm.send_leads(db, close_crm.top_unsent(db, account.id))
    except Exception as exc:
        return back_to_leads(f"Close error: {str(exc).splitlines()[0]}")
    return back_to_leads(f"Sent {s['sent']} leads to Close as Potential. {s['already_in_close']} were already in Close."
                         + (f" {s['failed']} failed." if s["failed"] else ""))


@app.post("/close/sync")
def close_sync(db: Session = Depends(get_session), account=Depends(current_account)):
    if account is None or not close_crm.configured():
        return back_to_leads("Close isn't connected: add CLOSE_API_KEY to .env.")
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
    return dict(zips=account.zip_list if account else [], industries=account.industry_list if account else [], spent=places.spent_this_month(db),
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
            for z in zips:
                for ind in industries:
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
                   cost_each=places.COST_PER_REQUEST_USD, has_close=close_crm.configured(), saved=request.query_params.get("saved"))
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
    extra = [i for i in (account.industry_list if account else []) if i not in INDUSTRIES]
    return render(request, "business.html", db, account, all_inds=INDUSTRIES, extra_inds=", ".join(extra),
                  default_desc=scoring.DEFAULT_DESCRIPTION, n_emailed=n_emailed,
                  gmail_ready=gmail.configured(), can_compose=bool(account and gmail.can_compose(account)), message=request.query_params.get("msg", ""))


@app.post("/business")
def save_business(description: str = Form(""), zips: str = Form(""), industries: list[str] = Form(default=[]),
                  extra_industries: str = Form(""), db: Session = Depends(get_session), account=Depends(current_account)):
    if account is None:
        raise HTTPException(400, "Create an account first")
    account.business_description = description.strip()[:3000]
    account.target_zips = ",".join(z for z in clean_list(zips) if z.isdigit() and len(z) == 5)
    account.industries = ",".join(clean_list(",".join([i for i in INDUSTRIES if i in industries]) + "," + extra_industries))
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
def gmail_callback(state: str = "", code: str = "", error: str = "", db: Session = Depends(get_session)):
    if error:
        return back_to_business(f"Gmail wasn't connected ({error}). If it says access blocked, N2's Google admin "
                                "needs to allow this app.")
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
                   spent=places.spent_this_month(db, account.id), limit=float(account.monthly_budget_usd),
                   overall=places.spent_this_month(db), overall_cap=MONTHLY_API_BUDGET_USD,
                   month=start.strftime("%B %Y"))
    return render(request, "usage.html", db, account, **ctx)


@app.get("/status")
def status_page(request: Request):
    return templates.TemplateResponse(request, "index.html", {"db": check_database()})


@app.get("/db-status")
def db_status(request: Request):
    """Just the database box, so the "Check again" button can refresh it (HTMX)."""
    return templates.TemplateResponse(request, "_db_status.html", {"db": check_database()})


@app.get("/health")
def health():
    db = check_database()
    return {"app": "ok", "database": "ok" if db["ok"] else "error"}

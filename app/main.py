"""Plug Employ Intelligence: accounts and lead tracker."""

from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import check_database, get_session
from app.models import STATUSES, Account, FollowUp, Lead, LeadNote

app = FastAPI(title="Plug Employ Intelligence")
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
def leads_list(request: Request, status: str = "", zip_code: str = "", sort: str = "follow_up",
               db: Session = Depends(get_session), account=Depends(current_account)):
    leads = []
    if account:
        q = (select(Lead).where(Lead.account_id == account.id)
             .options(selectinload(Lead.notes), selectinload(Lead.follow_ups)))
        if status:
            q = q.where(Lead.status == status)
        if zip_code:
            q = q.where(Lead.zip_code == zip_code)
        leads = list(db.scalars(q.order_by(Lead.business_name)))
        if sort == "follow_up":
            leads.sort(key=lambda l: l.next_follow_up.due_date if l.next_follow_up else date.max)
    zips = sorted({z for z in db.scalars(select(Lead.zip_code).where(Lead.account_id == (account.id if account else 0)))})
    return render(request, "leads.html", db, account, leads=leads, zips=zips,
                  f_status=status, f_zip=zip_code, sort=sort)


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
    return render(request, "lead_detail.html", db, account, lead=get_lead(db, account, lead_id))


@app.post("/leads/{lead_id}/status")
def set_status(lead_id: int, request: Request, status: str = Form(...),
               db: Session = Depends(get_session), account=Depends(current_account)):
    lead = get_lead(db, account, lead_id)
    if status not in STATUSES:
        raise HTTPException(400, "Unknown status")
    lead.status = status
    db.commit()
    return render(request, "_status.html", db, account, lead=lead, saved=True)


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

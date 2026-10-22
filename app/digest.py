"""Monday digest: a summary email Gmail sends from the owner to themselves. Nothing goes to leads."""

import base64
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import gmail
from app.config import APP_BASE_URL
from app.models import OUTCOMES, Account, FollowUp, Lead

SEND_WEEKDAY, SEND_HOUR = 0, 8  # Monday, 8 a.m. (this Mac's time)
QUIET_DAYS = 14


def build(db: Session, account: Account, now: datetime) -> tuple[str, str]:
    week_ago = now - timedelta(days=7)
    new_hot = db.scalars(select(Lead).where(Lead.account_id == account.id, Lead.created_at >= week_ago,
                                            Lead.score >= 8).order_by(Lead.score.desc()).limit(10)).all()
    due = db.scalars(select(FollowUp).where(FollowUp.account_id == account.id, FollowUp.done.is_(False),
                                            FollowUp.due_date <= date.today() + timedelta(days=7))
                     .order_by(FollowUp.due_date).limit(15)).all()
    quiet = db.scalars(select(Lead).where(Lead.account_id == account.id, Lead.status.not_in(OUTCOMES),
                                          Lead.last_emailed_at <= now - timedelta(days=QUIET_DAYS))
                       .order_by(Lead.score.desc().nulls_last()).limit(10)).all()
    lines = [f"Your week in Plug Employ Intelligence ({now.astimezone():%b %-d})", ""]
    lines += [f"NEW LEADS SCORED 8+ THIS WEEK ({len(new_hot)})"]
    lines += [f"  {l.score}/10  {l.business_name} ({l.industry}, {l.zip_code})" for l in new_hot] or ["  None this week."]
    lines += ["", f"FOLLOW-UPS DUE THIS WEEK ({len(due)})"]
    lines += [f"  {f.due_date:%a %b %-d}  {f.lead.business_name}: {f.task}" for f in due] or ["  Nothing due."]
    lines += ["", f"GONE QUIET: EMAILED {QUIET_DAYS}+ DAYS AGO, STILL OPEN ({len(quiet)})"]
    lines += [f"  {l.business_name} (last emailed {l.last_emailed_at.astimezone():%b %-d})" for l in quiet] or ["  None."]
    lines += ["", f"Open your leads: {APP_BASE_URL}/leads?sort=score"]
    subject = f"Your week: {len(new_hot)} hot new leads, {len(due)} follow-ups"
    return subject, "\n".join(lines)


def send_to_self(service, to: str, subject: str, body: str) -> None:
    """Send from the owner's Gmail to the owner. Kept separate so tests can replace it."""
    msg = EmailMessage()
    msg["To"], msg["From"], msg["Subject"] = to, to, subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def due_accounts(db: Session, now: datetime) -> list[int]:
    this_monday = (now - timedelta(days=(now.weekday() - SEND_WEEKDAY) % 7)).replace(
        hour=SEND_HOUR, minute=0, second=0, microsecond=0)
    if now < this_monday:
        this_monday -= timedelta(days=7)
    out = []
    for a in db.scalars(select(Account).where(Account.digest_enabled.is_(True), Account.gmail_token_enc != "")):
        if gmail.can_compose(a) and (a.digest_sent_at is None or a.digest_sent_at < this_monday):
            out.append(a.id)
    return out


def send(db: Session, account_id: int, now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    account = db.get(Account, account_id)
    subject, body = build(db, account, now)
    send_to_self(gmail.gmail_service(db, account), account.gmail_address, subject, body)
    account.digest_sent_at = now.astimezone(timezone.utc)
    db.commit()
    return subject

"""First-time setup: Claude suggests industries from the business description; a starter search."""

import anthropic
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import catalog, nightly, places, scoring
from app.config import ANTHROPIC_API_KEY
from app.models import Account, ApiUsage

STARTER_SEARCHES = 10
EST_SUGGEST_COST = 0.002


class Suggestion(BaseModel):
    industries: list[str]


def ask_claude(description: str) -> tuple[list[str], float]:
    """Kept separate so tests can replace it."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.parse(
        model=scoring.MODEL, max_tokens=3000, output_config={"effort": "low"},
        system=("You help a local advertising seller pick which kinds of local businesses to prospect. "
                "From the list given, choose the 8 to 20 business types most likely to buy what they sell. "
                "Use the exact names from the list. You may add up to 3 types not on the list if clearly needed."),
        messages=[{"role": "user", "content": f"What they sell and who they want:\n{description}\n\n"
                                              f"List of business types:\n{', '.join(catalog.ALL)}"}],
        output_format=Suggestion,
    )
    if response.parsed_output is None:
        return [], 0.0
    cost = (response.usage.input_tokens * scoring.PRICE_IN_PER_TOKEN
            + response.usage.output_tokens * scoring.PRICE_OUT_PER_TOKEN)
    return response.parsed_output.industries, cost


def suggest_industries(db: Session, account: Account, description: str) -> list[str]:
    if not description.strip() or not ANTHROPIC_API_KEY:
        return []
    places.check_budget(db, account.id, EST_SUGGEST_COST)
    names, cost = ask_claude(description.strip()[:3000])
    db.add(ApiUsage(account_id=account.id, service="claude_setup", detail="industry suggestions", est_cost_usd=cost))
    db.commit()
    lookup = {n.lower(): n for n in catalog.ALL}
    out = []
    for n in names:
        clean = " ".join(n.replace(",", "").split())[:80]
        name = lookup.get(clean.lower(), clean)
        if name and name not in out:
            out.append(name)
    return out[:25]


def finish(db: Session, account: Account) -> None:
    """Mark setup done and switch the nightly search on."""
    from datetime import datetime, timezone
    account.setup_done_at = datetime.now(timezone.utc)
    settings = nightly.get_settings(db, account.id)
    settings.enabled = True
    db.commit()


def starter_search(db: Session, account: Account) -> dict:
    """The first batch of leads, right now, so a new customer sees results immediately."""
    run = nightly.run_for_account(account.id, max_searches=STARTER_SEARCHES)
    return {"searches": run.searches, "added": run.added, "high": run.high_scores, "error": run.error}

"""What each plan actually buys — and the one place that decides it.

Until now the payment flow WROTE `user.plan` and nothing anywhere READ it. A grep across
the backend and the frontend found no check of any kind, so a ₹4,999 Enterprise payment and
a free signup got byte-for-byte the same product. Two things made that worse: the `plan`
column defaulted to `"starter"`, which is the name of the ₹499 plan, so every one of the 16
users in the database was already sitting on a paid plan they had never bought; and the two
monthly plans were charged as one-time orders with no expiry, so ₹1,499 once bought the plan
for ever.

This module is the single source of truth for all three: the price, what the plan allows,
and when it runs out. `payment_router` charges from here, `generation_router` gates from
here. Adding a plan or changing a limit is editing PLANS — nothing else.

The limits come from the public pricing page, so what is charged and what is delivered
cannot drift apart:

  free            a trial — 1 report, PDF only
  starter    ₹499 one-time — 3 reports, PDF only
  professional ₹1,499 / month — unlimited reports, PDF + Word + Excel
  enterprise ₹4,999 / month — the same, plus what is sold on top of it (seats,
                              white-label) which is not modelled here yet

`free` is not on the pricing page; a product nobody can try does not get bought, so one
report is allowed without paying. It is a deliberate addition, not something read off the
page.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from config import settings

logger = logging.getLogger(__name__)

# reports: how many PROJECTS may be generated; None means unlimited.
# exports: which download formats the plan may use.
# period_days: None for a one-time purchase that never lapses; a number for a plan that
#              must be paid again, after which the user falls back to `free`.
PLANS = {
    "free": {
        "label": "Free", "amount": 0, "usd_amount": 0, "period": "free", "period_days": None,
        "reports": 1, "exports": {"pdf"},
    },
    "starter": {
        "label": "Starter", "amount": 499, "usd_amount": 6, "period": "one-time", "period_days": None,
        "reports": 3, "exports": {"pdf"},
    },
    "professional": {
        "label": "Professional", "amount": 1499, "usd_amount": 149, "period": "monthly", "period_days": 30,
        "reports": None, "exports": {"pdf", "word", "excel"},
    },
    "enterprise": {
        "label": "Enterprise", "amount": 4999, "usd_amount": 499, "period": "monthly", "period_days": 30,
        "reports": None, "exports": {"pdf", "word", "excel"},
    },
}
FREE_PLAN = "free"
# Plans a user may actually buy — `free` is what you get without paying, so it is not one.
PURCHASABLE = {k: v for k, v in PLANS.items() if v["amount"] > 0}


def plan_spec(plan: str) -> dict:
    return PLANS.get((plan or "").strip().lower()) or PLANS[FREE_PLAN]


def effective_plan(user) -> str:
    """The plan this user is on RIGHT NOW.

    A monthly plan that has run out is not the plan the user is on, whatever the column
    says. Resolving that here — rather than in a nightly job — means an expiry can never be
    missed because a job did not run, and the answer is the same on every code path that
    asks.
    """
    plan = (getattr(user, "plan", None) or FREE_PLAN).strip().lower()
    if plan not in PLANS:
        return FREE_PLAN
    expires = getattr(user, "plan_expires_at", None)
    if expires and expires < datetime.utcnow():
        return FREE_PLAN
    return plan


def expiry_for(plan: str, paid_at: datetime | None = None):
    """When a plan just paid for runs out, or None if it never does."""
    days = plan_spec(plan)["period_days"]
    if not days:
        return None
    return (paid_at or datetime.utcnow()) + timedelta(days=days)


def reports_used(db, user) -> int:
    """How many of the user's projects have ever been generated.

    Counted per PROJECT, not per generation: regenerating a report — which the product
    encourages, and which the review-your-inputs flow depends on — must not eat the
    allowance. Three reports means three businesses, not three clicks.
    """
    from models.project_model import Project
    from models.report_model import Report
    return (db.query(Project.id)
              .join(Report, Report.project_id == Project.id)
              .filter(Project.user_id == user.id)
              .distinct().count())


def entitlements(db, user) -> dict:
    """Everything the UI needs to show a plan and a remaining allowance."""
    plan = effective_plan(user)
    spec = plan_spec(plan)
    used = reports_used(db, user)
    limit = spec["reports"]
    return {
        "plan": plan,
        "label": spec["label"],
        "expires_at": (getattr(user, "plan_expires_at", None).isoformat()
                       if getattr(user, "plan_expires_at", None) else None),
        "reports_limit": limit,
        "reports_used": used,
        "reports_left": None if limit is None else max(0, limit - used),
        "exports": sorted(spec["exports"]),
    }


def may_generate(db, user, project_id=None) -> tuple[bool, str]:
    """(allowed, why not). A project that has already been generated is always allowed
    through — that is a regeneration of something already paid for, not a new report."""
    if settings.UNLOCK_ALL:
        return True, ""
    plan = effective_plan(user)
    limit = plan_spec(plan)["reports"]
    if limit is None:
        return True, ""
    if project_id is not None:
        from models.report_model import Report
        if db.query(Report.id).filter(Report.project_id == project_id).first():
            return True, ""
    used = reports_used(db, user)
    if used < limit:
        return True, ""
    return False, (f"The {plan_spec(plan)['label']} plan covers {limit} "
                   f"report{'s' if limit != 1 else ''}, and "
                   f"{used} {'has' if used == 1 else 'have'} been generated. "
                   f"Upgrade to generate more.")


def may_export(user, kind: str) -> tuple[bool, str]:
    """(allowed, why not) for a download format: 'pdf', 'word' or 'excel'."""
    if settings.UNLOCK_ALL:
        return True, ""
    plan = effective_plan(user)
    spec = plan_spec(plan)
    if kind in spec["exports"]:
        return True, ""
    return False, (f"{kind.upper()} download is not included in the {spec['label']} plan. "
                   f"Upgrade to Professional to export Word and Excel.")


# Plans ordered by what they give you. Used to decide whether a purchase would leave someone
# WORSE OFF than they already are — which is the one outcome a payment must never produce.
RANK = {"free": 0, "starter": 1, "professional": 2, "enterprise": 3}


def grant_plan(user, plan: str, now: datetime | None = None) -> None:
    """Put a user on a plan they have just paid for.

    Two rules, both learned from what the old one-line version did:

    **It extends, it does not reset.** `expiry_for()` returns "30 days from now", so renewing
    early THREW AWAY the time already paid for — someone with 44 days left who bought another
    month came out with 30 and lost a fortnight. The new period is added to whatever is left.

    **It never shortens.** A one-time plan (no period) does not clear an expiry that is
    further out than nothing; and the caller is expected to have refused a downgrade before
    getting here, but if one arrives anyway the better expiry survives.
    """
    now = now or datetime.utcnow()
    plan = (plan or "").strip().lower()
    user.plan = plan
    days = plan_spec(plan)["period_days"]
    if not days:
        # A one-time plan does not expire. Clearing a longer expiry would be a reduction, so
        # it is only cleared when there is nothing to lose.
        current = getattr(user, "plan_expires_at", None)
        if not current or current <= now:
            user.plan_expires_at = None
        return
    base = getattr(user, "plan_expires_at", None)
    start = base if (base and base > now) else now
    user.plan_expires_at = start + timedelta(days=days)


def can_purchase(db, user, plan: str) -> tuple[bool, str]:
    """(allowed, why not) — would buying this plan actually give the customer anything?

    Taking money for something that leaves someone with LESS than they had is the worst thing
    a checkout can do, and it is what happened the first time a real payment went through
    here: an account on Professional bought Starter and dropped from unlimited reports with
    Word and Excel to three reports and PDF only. It paid to be downgraded.

    Refused BEFORE the order is created, so no money moves and there is nothing to refund.
    """
    plan = (plan or "").strip().lower()
    spec = PLANS.get(plan)
    if not spec:
        return False, f"Unknown plan: {plan}"
    current = effective_plan(user)

    if RANK.get(plan, 0) < RANK.get(current, 0):
        return False, (
            f"You are already on {plan_spec(current)['label']}, which includes more than "
            f"{spec['label']}. Buying this would reduce what you have. To move down, cancel "
            f"your current plan and buy this once it ends.")

    # Buying the same one-time plan again changes nothing: its allowance is a total for the
    # account, not a bundle that stacks, so the second payment would buy literally nothing.
    if plan == current and not spec["period_days"]:
        return False, (
            f"You are already on {spec['label']}, and it does not expire. Buying it again "
            f"would not add anything — upgrade to Professional for unlimited reports.")

    return True, ""

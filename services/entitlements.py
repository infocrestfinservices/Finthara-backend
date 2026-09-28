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

  free                not sold — 0 reports, no exports. What a signed-up account sits on
                      before it has ever paid.
  entrepreneur        ₹1,999 ONE-TIME — a top-up: each purchase adds 1 report credit
                      (Excel + Word + the online report). No team. Buy again for another.
  consultant_monthly  ₹11,000 / month — 20 reports per monthly cycle, 2 team seats.
  consultant_yearly   ₹119,988 / year (₹9,999 × 12) — the same 20 reports per month-long
                      cycle, 2 team seats, billed once a year.

`basic` and `advanced` are the plans this replaced. They are kept, NOT purchasable, only so
that an account that already paid for one keeps what it bought until it lapses, and so an
auto-pay mandate already running on one keeps being honoured by the webhook.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from config import settings

logger = logging.getLogger(__name__)

# reports: how many NEW projects may be generated per quota cycle; None means unlimited.
#          The Entrepreneur plan has no cycle — its allowance is the account's
#          `report_credits` (one per purchase), see may_generate.
# exports: which download formats the plan may use.
# period: "free" | "one_time" | "monthly" | "yearly" — how it is billed.
# period_days: None for a one-time purchase that never lapses; a number for a plan that
#              must be paid again, after which the user falls back to `free`.
# quota_cycles: how many report cycles one paid period is split into. The yearly plan is
#               12, so its 20 reports reset every 1/12 of the year — the same allowance per
#               month as the monthly plan, which it is just a cheaper way to pay for.
# seats: total people who may be on the account's team, INCLUDING the account holder. 1
# means "no team" (just you). Consultant & CA is "2 team seats" = the owner + 2 invited
# members. Team management (models/company_model.py, routers/team_router.py) is offered
# only on plans whose seats > 1. Matches the pricing page (landingData.js) — change both.
# topup: a one-time plan that can be bought again and again, each purchase adding a credit.
# legacy: no longer sold; kept so existing buyers keep what they paid for.
_ALL_EXPORTS = {"pdf", "word", "excel"}
PLANS = {
    "free": {
        "label": "Free", "amount": 0, "usd_amount": 0, "period": "free", "period_days": None,
        "reports": 0, "exports": set(), "seats": 1,
    },
    "entrepreneur": {
        "label": "Entrepreneur", "amount": 1999, "usd_amount": 20.99, "period": "one_time",
        "period_days": None, "reports": 1, "exports": _ALL_EXPORTS, "seats": 1, "topup": True,
    },
    "consultant_monthly": {
        "label": "Consultant & CA (Monthly)", "amount": 11000, "usd_amount": 114.99,
        "period": "monthly", "period_days": 30, "quota_cycles": 1,
        "reports": 20, "exports": _ALL_EXPORTS, "seats": 3,
    },
    "consultant_yearly": {
        "label": "Consultant & CA (Yearly)", "amount": 119988, "usd_amount": 1254.99,
        "period": "yearly", "period_days": 365, "quota_cycles": 12,
        "reports": 20, "exports": _ALL_EXPORTS, "seats": 3,
    },
    # ── legacy, not sold ──
    "basic": {
        "label": "Basic", "amount": 1999, "usd_amount": 20.99, "period": "monthly", "period_days": 30,
        "reports": None, "exports": _ALL_EXPORTS, "seats": 1, "legacy": True,
    },
    "advanced": {
        "label": "Advanced", "amount": 11000, "usd_amount": 114.99, "period": "monthly", "period_days": 30,
        "reports": None, "exports": _ALL_EXPORTS, "seats": 3, "legacy": True,
    },
}
FREE_PLAN = "free"
TOPUP_PLAN = "entrepreneur"
# Plans a user may actually buy — `free` is what you get without paying, and the legacy plans
# are no longer on sale.
PURCHASABLE = {k: v for k, v in PLANS.items() if v["amount"] > 0 and not v.get("legacy")}


def plan_spec(plan: str) -> dict:
    return PLANS.get((plan or "").strip().lower()) or PLANS[FREE_PLAN]


def seat_limit(user) -> int:
    """How many people (owner + members) this account's plan allows on its team."""
    return plan_spec(effective_plan(user)).get("seats", 1)


def team_enabled(user) -> bool:
    """Whether this account's current plan includes team seats at all."""
    return seat_limit(user) > 1


def effective_plan(user) -> str:
    """The plan this user is on RIGHT NOW.

    A monthly plan that has run out is not the plan the user is on, whatever the column
    says. Resolving that here — rather than in a nightly job — means an expiry can never be
    missed because a job did not run, and the answer is the same on every code path that
    asks. A lapsed plan falls back to Entrepreneur rather than Free while the account still
    holds unspent report credits, because those were bought outright and do not lapse.
    """
    plan = (getattr(user, "plan", None) or FREE_PLAN).strip().lower()
    if plan not in PLANS:
        plan = FREE_PLAN
    expires = getattr(user, "plan_expires_at", None)
    if expires and expires < datetime.utcnow():
        plan = FREE_PLAN
    if plan == FREE_PLAN and credits(user) > 0:
        return TOPUP_PLAN
    return plan


def credits(user) -> int:
    """Unspent one-time report credits (one per Entrepreneur purchase)."""
    return max(0, int(getattr(user, "report_credits", 0) or 0))


def expiry_for(plan: str, paid_at: datetime | None = None):
    """When a plan just paid for runs out, or None if it never does."""
    days = plan_spec(plan)["period_days"]
    if not days:
        return None
    return (paid_at or datetime.utcnow()) + timedelta(days=days)


def cycle_window(user, spec: dict, now: datetime | None = None) -> tuple[datetime, datetime]:
    """(start, end) of the report-quota cycle the user is in now.

    Counted BACK from the plan's expiry in steps of one cycle, so the cycles line up with what
    was paid for: a monthly plan's cycle is its paid month, and a yearly plan's 12 cycles tile
    its year exactly. Renewing early extends the expiry by whole periods, which keeps the
    tiling intact. A plan with no expiry (only possible by admin override) uses a rolling
    window ending now.
    """
    now = now or datetime.utcnow()
    length = timedelta(days=spec["period_days"] / (spec.get("quota_cycles") or 1))
    expires = getattr(user, "plan_expires_at", None)
    if not expires or expires <= now:
        return now - length, now
    k = max(1, math.ceil((expires - now) / length))
    start = expires - k * length
    return start, start + length


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


def reports_used_in_cycle(db, user, spec: dict) -> int:
    """New projects first generated inside the current quota cycle, NOT counting the ones
    paid for with a one-time credit (those were already paid for separately)."""
    from sqlalchemy import func
    from models.project_model import Project
    from models.report_model import Report
    start, _ = cycle_window(user, spec)
    return (db.query(Report.project_id)
              .join(Project, Report.project_id == Project.id)
              .filter(Project.user_id == user.id,
                      Project.paid_with_credit.isnot(True))
              .group_by(Report.project_id)
              .having(func.min(Report.created_at) >= start)
              .count())


def credit_reports_used(db, user) -> int:
    from models.project_model import Project
    return (db.query(Project.id)
              .filter(Project.user_id == user.id, Project.paid_with_credit.is_(True))
              .count())


def entitlements(db, user) -> dict:
    """Everything the UI needs to show a plan and a remaining allowance."""
    plan = effective_plan(user)
    spec = plan_spec(plan)
    left_credits = credits(user)
    out = {
        "plan": plan,
        "label": spec["label"],
        "period": spec["period"],
        "expires_at": (getattr(user, "plan_expires_at", None).isoformat()
                       if getattr(user, "plan_expires_at", None) else None),
        "report_credits": left_credits,
        "cycle_ends_at": None,
        "exports": sorted(spec["exports"]),
    }
    if spec["reports"] is None:
        out.update(reports_limit=None, reports_used=reports_used(db, user), reports_left=None)
    elif spec.get("period_days"):
        # A cycle plan. Any top-up credits the account also holds are spendable on top of the
        # cycle's allowance, so they count toward what is left.
        used = reports_used_in_cycle(db, user, spec)
        _, cycle_end = cycle_window(user, spec)
        out.update(reports_limit=spec["reports"], reports_used=used,
                   reports_left=max(0, spec["reports"] - used) + left_credits,
                   cycle_ends_at=cycle_end.isoformat())
    else:
        # Credits only (Entrepreneur, or Free): the allowance is what was bought.
        used = credit_reports_used(db, user)
        out.update(reports_limit=used + left_credits, reports_used=used,
                   reports_left=left_credits)
    return out


def _already_paid_for(db, project_id) -> bool:
    """A project that has a report, or that a credit was already spent on (e.g. its first
    generation failed and is being retried), is never charged again."""
    if project_id is None:
        return False
    from models.project_model import Project
    from models.report_model import Report
    if db.query(Report.id).filter(Report.project_id == project_id).first():
        return True
    return bool(db.query(Project.id)
                  .filter(Project.id == project_id, Project.paid_with_credit.is_(True))
                  .first())


def _decide(db, user, project_id) -> tuple[bool, str, bool]:
    """(allowed, why not, needs a credit). The single decision both may_generate (a
    read-only check) and claim_generation (which spends) are built on."""
    if settings.UNLOCK_ALL:
        return True, "", False
    if _already_paid_for(db, project_id):
        return True, "", False
    plan = effective_plan(user)
    spec = plan_spec(plan)
    if spec["reports"] is None:
        return True, "", False
    if spec.get("period_days"):
        used = reports_used_in_cycle(db, user, spec)
        if used < spec["reports"]:
            return True, "", False
        if credits(user) > 0:
            return True, "", True
        _, end = cycle_window(user, spec)
        return False, (f"The {spec['label']} plan covers {spec['reports']} reports per cycle, "
                       f"and {used} have been generated in this one. It resets on "
                       f"{end.strftime('%d %b %Y')} — or buy an Entrepreneur report to "
                       f"generate one more now."), False
    if credits(user) > 0:
        return True, "", True
    if plan == TOPUP_PLAN:
        return False, ("Your Entrepreneur report has been used. Buy another Entrepreneur "
                       "report, or move to Consultant & CA for 20 reports a month."), False
    return False, "Choose a plan to generate your report.", False


def may_generate(db, user, project_id=None) -> tuple[bool, str]:
    """(allowed, why not). Read-only: spends nothing. A project that has already been
    generated is always allowed through — that is a regeneration of something already paid
    for, not a new report."""
    allowed, why, _ = _decide(db, user, project_id)
    return allowed, why


def claim_generation(db, user, project) -> tuple[bool, str]:
    """may_generate, and if the generation is going to be paid for with a one-time credit,
    spend that credit now and mark the project as paid.

    Spent at the START rather than when the report lands, and recorded on the project, so a
    generation that fails can be retried on the same project without spending a second
    credit — and the user row is locked so two tabs cannot each spend the same last credit on
    different projects.
    """
    allowed, why, needs_credit = _decide(db, user, project.id)
    if not allowed or not needs_credit:
        return allowed, why
    from models.user_model import User
    locked = db.query(User).filter(User.id == user.id).with_for_update().first()
    if not locked or credits(locked) < 1:
        return False, "Your report credit has already been used. Buy another to continue."
    locked.report_credits = credits(locked) - 1
    project.paid_with_credit = True
    db.commit()
    logger.info("entitlements: user %s spent a report credit on project %s (%s left)",
                locked.id, project.id, locked.report_credits)
    return True, ""


def may_export(user, kind: str) -> tuple[bool, str]:
    """(allowed, why not) for a download format: 'pdf', 'word' or 'excel'."""
    if settings.UNLOCK_ALL:
        return True, ""
    plan = effective_plan(user)
    spec = plan_spec(plan)
    if kind in spec["exports"]:
        return True, ""
    return False, (f"{kind.upper()} download is not included in the {spec['label']} plan. "
                   f"Choose a plan to export Word and Excel.")


# Plans ordered by what they give you. Used to decide whether a purchase would leave someone
# WORSE OFF than they already are — which is the one outcome a payment must never produce.
RANK = {"free": 0, "entrepreneur": 1, "basic": 1,
        "consultant_monthly": 2, "consultant_yearly": 2, "advanced": 2}


def grant_plan(user, plan: str, now: datetime | None = None) -> None:
    """Put a user on a plan they have just paid for.

    Rules, learned from what the old one-line version did:

    **A top-up adds, it does not replace.** An Entrepreneur purchase adds one report credit.
    It only moves the account onto Entrepreneur if the account has nothing better right now —
    buying an extra report while on Consultant & CA must not end the Consultant plan.

    **It extends, it does not reset.** `expiry_for()` returns "30 days from now", so renewing
    early THREW AWAY the time already paid for — someone with 44 days left who bought another
    month came out with 30 and lost a fortnight. The new period is added to whatever is left.

    **It never shortens.** A one-time plan does not clear an expiry that is further out than
    nothing; and the caller is expected to have refused a downgrade before getting here, but
    if one arrives anyway the better expiry survives.
    """
    now = now or datetime.utcnow()
    plan = (plan or "").strip().lower()
    spec = plan_spec(plan)
    if spec.get("topup"):
        better_plan_running = RANK.get(effective_plan(user), 0) > RANK.get(plan, 0)
        user.report_credits = credits(user) + 1
        if not better_plan_running:
            user.plan = plan
            current = getattr(user, "plan_expires_at", None)
            if not current or current <= now:
                user.plan_expires_at = None
        return
    user.plan = plan
    days = spec["period_days"]
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
    here: an account on a higher plan bought a lower one and dropped what it already had.
    It paid to be downgraded.

    Refused BEFORE the order is created, so no money moves and there is nothing to refund.
    """
    plan = (plan or "").strip().lower()
    spec = PURCHASABLE.get(plan)
    if not spec:
        return False, f"Unknown plan: {plan}"
    # A top-up only ever ADDS a report credit, whatever plan the account is on.
    if spec.get("topup"):
        return True, ""
    current = effective_plan(user)

    if RANK.get(plan, 0) < RANK.get(current, 0):
        return False, (
            f"You are already on {plan_spec(current)['label']}, which includes more than "
            f"{spec['label']}. Buying this would reduce what you have. To move down, cancel "
            f"your current plan and buy this once it ends.")

    return True, ""

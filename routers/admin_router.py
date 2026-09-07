"""The staff view of the product: who signed up, what they paid, what they generated.

None of this existed. Every question about the business — how many users, who is on a paid
plan, which orders never completed, whether an industry keeps failing to generate — had to be
answered by opening a Python shell against the database. That is fine once and useless as a
way to run something.

Every route here depends on `get_admin_user`, which 404s for everyone else, so the whole
prefix is invisible to a normal account. The only route that WRITES is the plan override,
because support work needs it (a payment that did not register, a refund, extending a trial);
it records who did it in the log, and it cannot grant admin — that is deliberate, and the
reason `is_admin` is only settable with grant_admin.py against the database.

Read paths are deliberately paginated and bounded: an admin page that tries to render every
row is the page that stops opening once the product works.
"""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_admin_user
from models.invoice_model import Invoice
from models.payment_model import Payment
from models.project_model import Project
from models.report_model import Report
from models.user_model import User
from services.entitlements import (PLANS, RANK, effective_plan, expiry_for,
                                   plan_spec)

logger = logging.getLogger("admin")
router = APIRouter(prefix="/admin", tags=["Admin"], dependencies=[Depends(get_admin_user)])

MAX_PAGE = 200


def _page(q, limit: int, offset: int):
    return q.limit(min(limit, MAX_PAGE)).offset(offset).all()


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    """The numbers the team actually asks for, in one call."""
    users_total = db.query(func.count(User.id)).scalar() or 0
    # Counted from effective_plan, not the raw column: a lapsed monthly plan is not a paying
    # customer however the row reads, and a dashboard that says otherwise is worse than none.
    by_plan = {}
    on_paid_plan = 0
    for u in db.query(User).all():
        p = effective_plan(u)
        by_plan[p] = by_plan.get(p, 0) + 1
        if plan_spec(p)["amount"] > 0:
            on_paid_plan += 1
    # Being ON a paid plan and having PAID for one are different things, and conflating them
    # is how a dashboard reports 16 paying customers against ₹0 of revenue. Everyone here was
    # grandfathered onto Professional when entitlements were introduced; none of them paid.
    ever_paid = (db.query(func.count(func.distinct(Payment.user_id)))
                   .filter(Payment.status == "paid").scalar() or 0)

    projects_total = db.query(func.count(Project.id)).scalar() or 0
    generated = (db.query(func.count(func.distinct(Report.project_id))).scalar() or 0)

    paid_q = db.query(Payment).filter(Payment.status == "paid")
    revenue = db.query(func.coalesce(func.sum(Payment.amount), 0)).filter(
        Payment.status == "paid").scalar() or 0

    # Orders that were started and never finished. Two of these have been sitting unnoticed
    # since the checkout was first wired, which is exactly the sort of thing this page is for.
    stuck = db.query(func.count(Payment.id)).filter(Payment.status == "created").scalar() or 0

    return {
        "users": {"total": users_total, "on_paid_plan": on_paid_plan,
                  "ever_paid": ever_paid, "by_plan": by_plan},
        "projects": {"total": projects_total, "generated": generated,
                     "never_generated": max(0, projects_total - generated)},
        "revenue": {"total": float(revenue), "paid_orders": paid_q.count(),
                    "incomplete_orders": stuck},
    }


@router.get("/users")
def list_users(db: Session = Depends(get_db), q: str | None = None,
               plan: str | None = None, limit: int = Query(50, le=MAX_PAGE), offset: int = 0):
    """Users with the two things a plan row is meaningless without: what they are ENTITLED
    to right now, and how much of it they have used."""
    query = db.query(User)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(User.email.ilike(like), User.full_name.ilike(like)))
    total = query.count()
    rows = _page(query.order_by(User.id.desc()), limit, offset)

    # One grouped query for the report counts rather than one per user — the per-user version
    # is invisible at 16 users and is the reason admin pages die at 5,000.
    counts = dict(db.query(Project.user_id, func.count(func.distinct(Report.project_id)))
                    .join(Report, Report.project_id == Project.id)
                    .group_by(Project.user_id).all())
    projects = dict(db.query(Project.user_id, func.count(Project.id))
                      .group_by(Project.user_id).all())

    out = []
    for u in rows:
        eff = effective_plan(u)
        spec = plan_spec(eff)
        if plan and eff != plan:
            continue
        out.append({
            "id": u.id, "email": u.email, "full_name": u.full_name,
            "plan_column": u.plan, "plan": eff,
            "lapsed": bool(u.plan and u.plan != eff),
            "plan_expires_at": u.plan_expires_at.isoformat() if u.plan_expires_at else None,
            "is_admin": bool(u.is_admin), "is_verified": bool(u.is_verified),
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "projects": projects.get(u.id, 0),
            "reports_used": counts.get(u.id, 0),
            "reports_limit": spec["reports"],
        })
    return {"total": total, "limit": limit, "offset": offset, "users": out}


class PlanOverride(BaseModel):
    plan: str
    # Days from now. None uses the plan's own period, which is what support wants almost
    # always; an explicit number is for the cases that are not almost always.
    days: int | None = None
    reason: str | None = None


@router.patch("/users/{user_id}/plan")
def set_plan(user_id: int, body: PlanOverride, db: Session = Depends(get_db),
             admin: User = Depends(get_admin_user)):
    """Move a user onto a plan by hand — a payment that did not register, a goodwill
    extension, a refund taking someone back down."""
    plan = (body.plan or "").strip().lower()
    if plan not in PLANS:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {body.plan}")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    was, was_exp = user.plan, user.plan_expires_at
    user.plan = plan
    if body.days is not None:
        from datetime import timedelta
        user.plan_expires_at = (datetime.utcnow() + timedelta(days=body.days)
                                if body.days > 0 else None)
    else:
        user.plan_expires_at = expiry_for(plan)
    db.commit()
    # Money moved without a payment record, so the log is the only trace there will be.
    logger.warning("admin: %s changed user %s (%s) from %r/%s to %r/%s — reason: %s",
                   admin.email, user.id, user.email, was, was_exp, user.plan,
                   user.plan_expires_at, body.reason or "(none given)")
    return {"id": user.id, "email": user.email, "plan": user.plan,
            "plan_expires_at": (user.plan_expires_at.isoformat()
                                if user.plan_expires_at else None)}


@router.get("/payments")
def list_payments(db: Session = Depends(get_db), status_filter: str | None = Query(None, alias="status"),
                  limit: int = Query(50, le=MAX_PAGE), offset: int = 0):
    """Every order, finished or not. The unfinished ones are the point: they are invisible
    everywhere else, and a run of them means the checkout is broken for real people."""
    query = db.query(Payment, User).join(User, User.id == Payment.user_id)
    if status_filter:
        query = query.filter(Payment.status == status_filter)
    total = query.count()
    rows = _page(query.order_by(Payment.id.desc()), limit, offset)
    return {
        "total": total, "limit": limit, "offset": offset,
        "payments": [{
            "id": p.id, "user_id": u.id, "email": u.email,
            "plan": p.plan, "amount": float(p.amount or 0), "currency": p.currency,
            "status": p.status, "order_id": p.razorpay_order_id,
            "payment_id": p.razorpay_payment_id,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "paid_at": p.paid_at.isoformat() if p.paid_at else None,
        } for p, u in rows],
    }


@router.get("/projects")
def list_projects(db: Session = Depends(get_db), q: str | None = None,
                  industry: str | None = None, generated: bool | None = None,
                  limit: int = Query(50, le=MAX_PAGE), offset: int = 0):
    """Projects with whether a report ever came out of them. A project that has sat
    ungenerated, or an industry where several have, is a failure nobody would otherwise see —
    the user just stops trying."""
    # OUTER joins on both sides. The nine oldest projects predate authentication and have a
    # NULL user_id; an inner join silently dropped them, so the page showed 31 of 40 and
    # looked complete. A row missing from an admin list is worse than an ugly one.
    query = (db.query(Project, User, Report)
               .outerjoin(User, User.id == Project.user_id)
               .outerjoin(Report, Report.project_id == Project.id))
    if q:
        query = query.filter(Project.title.ilike(f"%{q.strip()}%"))
    if industry:
        query = query.filter(Project.industry == industry)
    if generated is True:
        query = query.filter(Report.id.isnot(None))
    elif generated is False:
        query = query.filter(Report.id.is_(None))
    # Counted over PROJECTS, not over join rows: a project with two report rows is still
    # one project, and counting rows would quietly inflate the total.
    total = query.with_entities(func.count(func.distinct(Project.id))).scalar() or 0
    rows = _page(query.order_by(Project.id.desc()), limit, offset)
    return {
        "total": total, "limit": limit, "offset": offset,
        "projects": [{
            "id": p.id, "title": p.title, "industry": p.industry,
            "sub_industry": p.sub_industry, "purpose": p.purpose,
            "report_format": p.report_format, "project_cost": p.project_cost,
            "email": getattr(u, "email", None) or "(no account)",
            "generated": r is not None,
            "report_status": getattr(r, "status", None),
            "created_at": p.created_at.isoformat() if p.created_at else None,
        } for p, u, r in rows],
    }


@router.get("/industries")
def industry_breakdown(db: Session = Depends(get_db)):
    """Projects and successful generations per industry, worst first.

    Grouped by the industry's OPERATING-MODEL SLUG, not by the raw text of the field.
    `Project.industry` is free text, so the same industry arrives under whatever the user or
    the AI called it that day — the database holds 21 distinct strings for 11 industries, with
    "Retail" and "Retail & E-Commerce" counted apart, and "Hospitality", "Hospitality &
    Tourism" and "Tourism & Hospitality" counted three ways. Split like that the numbers
    answer nothing.

    `get_operating_model` already resolves those aliases — it is what decides which workbook
    and which calculations a project gets — so using it here means this page groups a project
    exactly the way the engine treats it. Two different answers to "what industry is this?"
    would be worse than none.

    Each row carries the raw `variants` that fed it, because an admin looking at a merged
    count has to be able to check that nothing was merged that should not have been.
    """
    from financial_engine.industry_calc.operating_models import get_operating_model

    generated = {pid for (pid,) in db.query(Report.project_id).distinct().all()}
    buckets: dict[str, dict] = {}
    for pid, raw in db.query(Project.id, Project.industry).all():
        m = get_operating_model(raw or "")
        # An unset industry is not an industry called "General Business" — it is a gap, and
        # folding it into `other` would hide projects that were never classified at all.
        key = m.key if m else "_unset"
        label = m.display_name if m else "Not set"
        b = buckets.setdefault(key, {"key": key, "industry": label, "projects": 0,
                                     "generated": 0, "variants": {}})
        b["projects"] += 1
        if pid in generated:
            b["generated"] += 1
        if raw:
            b["variants"][raw] = b["variants"].get(raw, 0) + 1

    rows = []
    for b in buckets.values():
        rows.append({
            "key": b["key"], "industry": b["industry"],
            "projects": b["projects"], "generated": b["generated"],
            "failed": b["projects"] - b["generated"],
            "variants": [{"name": n, "count": c}
                         for n, c in sorted(b["variants"].items(), key=lambda kv: -kv[1])],
        })
    rows.sort(key=lambda r: (-r["failed"], -r["projects"], r["industry"]))
    return {"industries": rows}


# ── coupons ────────────────────────────────────────────────────────────────────
# Creating a discount is giving money away, so it belongs behind the same door as changing
# someone's plan: staff only, and recorded against the admin who did it.

class CouponIn(BaseModel):
    code: str
    kind: str = "percent"                 # percent | flat
    value: float
    description: str | None = None
    applies_to: list[str] | None = None   # plan keys; empty/None means every paid plan
    max_redemptions: int | None = None
    per_user_limit: int | None = 1
    valid_from: datetime | None = None
    valid_until: datetime | None = None


@router.get("/coupons")
def list_coupons(db: Session = Depends(get_db)):
    """Every code with how much of it is left — the number that decides whether to issue
    another one or stop this one."""
    from models.coupon_model import Coupon, CouponRedemption
    rows = db.query(Coupon).order_by(Coupon.id.desc()).all()
    saved = dict(db.query(CouponRedemption.coupon_id,
                          func.coalesce(func.sum(CouponRedemption.discount), 0))
                   .group_by(CouponRedemption.coupon_id).all())
    now = datetime.utcnow()
    return {"coupons": [{
        "id": c.id, "code": c.code, "description": c.description,
        "kind": c.kind, "value": c.value,
        "applies_to": [p for p in (c.applies_to or "").split(",") if p],
        "used_count": c.used_count or 0, "max_redemptions": c.max_redemptions,
        "per_user_limit": c.per_user_limit,
        "valid_from": c.valid_from.isoformat() if c.valid_from else None,
        "valid_until": c.valid_until.isoformat() if c.valid_until else None,
        "active": bool(c.active),
        # "active" is the switch; this is whether it would actually work right now, which is
        # what someone about to hand the code to a customer needs to know.
        "usable_now": bool(
            c.active
            and (not c.valid_from or c.valid_from <= now)
            and (not c.valid_until or c.valid_until >= now)
            and (c.max_redemptions is None or (c.used_count or 0) < c.max_redemptions)),
        "discount_given": float(saved.get(c.id, 0)),
        "created_by": c.created_by,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    } for c in rows]}


@router.post("/coupons")
def create_coupon(body: CouponIn, db: Session = Depends(get_db),
                  admin: User = Depends(get_admin_user)):
    from models.coupon_model import Coupon
    from services.coupons import normalise

    code = normalise(body.code)
    if not code:
        raise HTTPException(status_code=400, detail="A code is required.")
    if body.kind not in ("percent", "flat"):
        raise HTTPException(status_code=400, detail="kind must be 'percent' or 'flat'.")
    if body.value <= 0:
        raise HTTPException(status_code=400, detail="The value must be more than zero.")
    if body.kind == "percent" and body.value > 100:
        raise HTTPException(status_code=400, detail="A percentage cannot exceed 100.")
    if db.query(Coupon).filter(Coupon.code == code).first():
        raise HTTPException(status_code=409, detail=f"{code} already exists.")

    bad = [p for p in (body.applies_to or []) if p.strip().lower() not in PLANS]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown plan(s): {', '.join(bad)}")

    c = Coupon(code=code, kind=body.kind, value=body.value,
               description=body.description,
               applies_to=",".join(p.strip().lower() for p in (body.applies_to or [])) or None,
               max_redemptions=body.max_redemptions,
               per_user_limit=body.per_user_limit,
               valid_from=body.valid_from, valid_until=body.valid_until,
               created_by=admin.email)
    db.add(c)
    db.commit()
    logger.warning("admin: %s created coupon %s (%s %s)", admin.email, code, body.value,
                   body.kind)
    return {"id": c.id, "code": c.code}


@router.patch("/coupons/{coupon_id}")
def set_coupon_active(coupon_id: int, active: bool, db: Session = Depends(get_db),
                      admin: User = Depends(get_admin_user)):
    """Turn a code off (or back on). Deliberately not a delete: the redemptions point at it,
    and the record of who was given what discount has to survive the code being retired."""
    from models.coupon_model import Coupon
    c = db.get(Coupon, coupon_id)
    if not c:
        raise HTTPException(status_code=404, detail="Coupon not found")
    c.active = bool(active)
    db.commit()
    logger.warning("admin: %s %s coupon %s", admin.email,
                   "enabled" if active else "disabled", c.code)
    return {"id": c.id, "code": c.code, "active": c.active}


# ── dashboard ──────────────────────────────────────────────────────────────────

@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), days: int = Query(90, ge=7, le=730)):
    """The shape of the business over time, in one call.

    Bucketed WEEKLY rather than daily. At this size a daily series is 90 columns of mostly
    zero, which reads as "nothing is happening" even in a week when several things did; a
    week is also the unit the numbers are actually discussed in.

    Buckets are built for every week in the window and then filled, so a quiet week is a
    visible zero rather than a gap the line hops over — a chart that silently omits empty
    periods misstates the slope between the points it does draw.
    """
    from models.coupon_model import CouponRedemption

    now = datetime.utcnow()
    start = now - timedelta(days=days)

    # Monday of the week a timestamp falls in — the bucket key.
    def week_of(dt):
        d = (dt or now).date()
        return d - timedelta(days=d.weekday())

    weeks = []
    cur = week_of(start)
    last = week_of(now)
    while cur <= last:
        weeks.append(cur)
        cur = cur + timedelta(days=7)
    buckets = {w: {"week": w.isoformat(), "signups": 0, "reports": 0, "revenue": 0.0}
               for w in weeks}

    def add(dt, field, amount=1):
        if not dt or dt < start:
            return
        b = buckets.get(week_of(dt))
        if b:
            b[field] += amount

    for (created,) in db.query(User.created_at).all():
        add(created, "signups")
    # Counted on the REPORT row, not the project: a project created in one week and generated
    # in another belongs to the week the work actually happened in.
    for (created,) in db.query(Report.created_at).all():
        add(created, "reports")
    for paid, amount in db.query(Payment.paid_at, Payment.amount).filter(
            Payment.status == "paid").all():
        add(paid, "revenue", float(amount or 0))

    series = [buckets[w] for w in weeks]

    # The funnel, each step counted independently against the SAME population — cumulative
    # "of those who did the previous step" reads better but hides someone who paid without
    # ever generating, which is a thing worth seeing.
    total_users = db.query(func.count(User.id)).scalar() or 0
    with_project = (db.query(func.count(func.distinct(Project.user_id)))
                      .filter(Project.user_id.isnot(None)).scalar() or 0)
    with_report = (db.query(func.count(func.distinct(Project.user_id)))
                     .join(Report, Report.project_id == Project.id)
                     .filter(Project.user_id.isnot(None)).scalar() or 0)
    paid_users = (db.query(func.count(func.distinct(Payment.user_id)))
                    .filter(Payment.status == "paid").scalar() or 0)

    by_plan = {}
    for u in db.query(User).all():
        p = effective_plan(u)
        by_plan[p] = by_plan.get(p, 0) + 1

    discount_given = float(db.query(func.coalesce(
        func.sum(CouponRedemption.discount), 0)).scalar() or 0)

    return {
        "window_days": days,
        "series": series,
        "funnel": [
            {"step": "Signed up", "count": total_users},
            {"step": "Started a project", "count": with_project},
            {"step": "Generated a report", "count": with_report},
            {"step": "Paid", "count": paid_users},
        ],
        "plans": [{"plan": k, "label": plan_spec(k)["label"], "count": v}
                  for k, v in sorted(by_plan.items(),
                                     key=lambda kv: -RANK.get(kv[0], 0))],
        "totals": {
            "revenue": float(db.query(func.coalesce(func.sum(Payment.amount), 0))
                               .filter(Payment.status == "paid").scalar() or 0),
            "discount_given": discount_given,
            "users": total_users,
            "reports": db.query(func.count(func.distinct(Report.project_id))).scalar() or 0,
        },
    }


# ── roles ──────────────────────────────────────────────────────────────────────
# There are two roles: `admin` (this console) and `user` (everyone else), held on the user
# row as `is_admin`.
#
# This endpoint reverses an earlier decision. Admin was deliberately grantable ONLY by
# grant_admin.py against the database, on the reasoning that an endpoint which hands out
# admin is one authorisation bug away from anyone granting it to themselves. Putting it on
# the web was asked for, so the reasoning has to be answered rather than dropped — three
# locks do that:
#
#   1. only an admin can reach it at all (get_admin_user, which 404s for everyone else);
#   2. an admin cannot demote THEMSELVES, and the last admin cannot be demoted by anyone —
#      either would lock the whole team out of a console that can only be reopened by
#      someone with database access;
#   3. every change is logged with who did it, because a role change leaves no other trace.
#
# grant_admin.py stays, and is still the only way back in if the last admin is ever lost.

class RoleChange(BaseModel):
    is_admin: bool
    reason: str | None = None


@router.get("/roles")
def list_roles(db: Session = Depends(get_db)):
    """Who holds which role. Admins first — the short list is the one being audited."""
    rows = db.query(User).order_by(User.is_admin.desc(), User.id.desc()).all()
    admins = [u for u in rows if u.is_admin]
    return {
        "admin_count": len(admins),
        "roles": [
            {"id": "admin", "label": "Admin",
             "description": "Full access to this console: every user, payment, project and "
                            "coupon, and the ability to change plans and roles.",
             "count": len(admins)},
            {"id": "user", "label": "User",
             "description": "The normal product. No access to this console at all — every "
                            "/admin address answers 404.",
             "count": len(rows) - len(admins)},
        ],
        "users": [{
            "id": u.id, "email": u.email, "full_name": u.full_name,
            "role": "admin" if u.is_admin else "user",
            "plan": effective_plan(u),
            "created_at": u.created_at.isoformat() if u.created_at else None,
        } for u in rows],
    }


@router.patch("/users/{user_id}/role")
def set_role(user_id: int, body: RoleChange, db: Session = Depends(get_db),
             admin: User = Depends(get_admin_user)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not body.is_admin:
        if user.id == admin.id:
            raise HTTPException(
                status_code=400,
                detail="You cannot remove your own admin access — you would be locked out "
                       "of this console immediately. Ask another admin to do it.")
        remaining = db.query(func.count(User.id)).filter(User.is_admin.is_(True)).scalar() or 0
        if remaining <= 1:
            raise HTTPException(
                status_code=400,
                detail="This is the only admin left. Removing it would lock everyone out of "
                       "the console, and it could only be reopened from the database.")

    was = bool(user.is_admin)
    if was == bool(body.is_admin):
        return {"id": user.id, "email": user.email,
                "role": "admin" if user.is_admin else "user", "changed": False}

    user.is_admin = bool(body.is_admin)
    db.commit()
    logger.warning("admin: %s %s admin access %s %s — reason: %s",
                   admin.email, "granted" if user.is_admin else "removed",
                   "to" if user.is_admin else "from", user.email,
                   body.reason or "(none given)")
    return {"id": user.id, "email": user.email,
            "role": "admin" if user.is_admin else "user", "changed": True}


@router.get("/repeat-buyers")
def repeat_buyers(db: Session = Depends(get_db)):
    """Customers who have paid more than once, and what they have spent in total.

    A separate view because the payments list is ordered by time: someone who has bought four
    times appears as four unrelated rows scattered through it, and the fact that they are the
    same person — which is the interesting fact — is invisible.
    """
    rows = (db.query(Payment.user_id, User.email,
                     func.count(Payment.id).label("orders"),
                     func.coalesce(func.sum(Payment.amount), 0).label("total"),
                     func.max(Payment.paid_at).label("last"))
              .join(User, User.id == Payment.user_id)
              .filter(Payment.status == "paid")
              .group_by(Payment.user_id, User.email)
              .having(func.count(Payment.id) > 1)
              .order_by(func.count(Payment.id).desc())
              .all())
    return {"buyers": [{
        "user_id": r.user_id, "email": r.email, "orders": r.orders,
        "total": float(r.total or 0),
        "last_paid_at": r.last.isoformat() if r.last else None,
    } for r in rows]}


# ── invoices ───────────────────────────────────────────────────────────────────
# Grouped by BUYER rather than one flat list, on purpose: support looking up a customer wants
# everything that customer has ever paid in one place, and a flat list ordered by date scatters
# one buyer's five invoices through however many other people paid in between.

MAX_INVOICES_SCANNED = 5000   # a bound on the underlying query, not on what a buyer sees


def _payment_reference(payment: Payment | None) -> str:
    if not payment:
        return "—"
    if float(payment.amount or 0) == 0 and payment.coupon_code:
        return "Coupon — no charge"
    if payment.gateway == "paypal":
        return payment.paypal_capture_id or payment.paypal_order_id or "—"
    return payment.razorpay_payment_id or payment.razorpay_order_id or "—"


def _payment_method(payment: Payment | None) -> str:
    if not payment:
        return "—"
    if float(payment.amount or 0) == 0 and payment.coupon_code:
        return "Coupon — no charge"
    return "PayPal" if payment.gateway == "paypal" else "Razorpay"


@router.get("/invoices")
def admin_invoices(db: Session = Depends(get_db), q: str | None = None,
                   date_from: datetime | None = None, date_to: datetime | None = None,
                   limit: int = Query(25, le=100), offset: int = 0):
    """Every paid invoice, grouped by who bought it. `limit`/`offset` paginate the BUYER
    list, not the invoice rows — a buyer with twelve invoices is one row of pagination, not
    twelve."""
    query = (db.query(Invoice, Payment)
               .outerjoin(Payment, Payment.id == Invoice.payment_id)
               .filter(Invoice.status == "paid"))
    if date_from:
        query = query.filter(Invoice.issued_at >= date_from)
    if date_to:
        query = query.filter(Invoice.issued_at <= date_to)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(
            Invoice.invoice_number.ilike(like),
            Invoice.customer_email.ilike(like),
            Payment.razorpay_payment_id.ilike(like),
            Payment.razorpay_order_id.ilike(like),
            Payment.paypal_capture_id.ilike(like),
            Payment.paypal_order_id.ilike(like),
        ))

    rows = query.order_by(Invoice.issued_at.desc()).limit(MAX_INVOICES_SCANNED).all()

    currency_totals: dict[str, float] = {}
    buyers: dict[int, dict] = {}
    order: list[int] = []   # buyer_id in first-seen order == most-recent-invoice-first
    for inv, payment in rows:
        currency_totals[inv.currency] = round(
            currency_totals.get(inv.currency, 0.0) + float(inv.gross or 0), 2)

        uid = inv.user_id
        if uid not in buyers:
            buyers[uid] = {
                "user_id": uid, "email": inv.customer_email,
                "name": inv.customer_name, "invoices": [],
                "total_paid": {},
            }
            order.append(uid)
        b = buyers[uid]
        b["total_paid"][inv.currency] = round(
            b["total_paid"].get(inv.currency, 0.0) + float(inv.gross or 0), 2)
        b["invoices"].append({
            "id": inv.id,
            "invoice_number": inv.invoice_number,
            "issued_at": inv.issued_at.isoformat() if inv.issued_at else None,
            "plan": inv.plan,
            "description": inv.description,
            "currency": inv.currency,
            "list_amount": round(float(inv.gross or 0) + float(inv.discount or 0), 2),
            "discount": float(inv.discount or 0),
            "paid_amount": float(inv.gross or 0),
            "coupon_code": inv.coupon_code,
            "payment_method": _payment_method(payment),
            "payment_reference": _payment_reference(payment),
        })

    total_buyers = len(order)
    page_ids = order[offset:offset + limit]
    return {
        "buyer_count": total_buyers,
        "invoice_count": len(rows),
        "currency_totals": currency_totals,
        "limit": limit, "offset": offset,
        "buyers": [buyers[uid] for uid in page_ids],
    }

"""Auto-pay: recurring billing through Razorpay Subscriptions.

The one-time Orders flow charges once and stops. That is right for Starter, which is sold as
a one-time purchase, and wrong for Professional and Enterprise, which are sold per month and
were being charged once and granted for ever. Subscriptions is Razorpay taking the mandate
from the customer and debiting it each cycle; we hear about each debit on a webhook and move
the plan's expiry to whatever date THEY say the customer is now paid up to.

Three things drive the design.

**Razorpay owns the dates.** Every cycle boundary comes from `current_end` on the event, not
from adding 30 days here. A retried charge, a customer on a different timezone, a cycle
Razorpay shifted — all of those make a locally-computed date wrong, and a wrong expiry either
locks out a paying customer or gives away a month.

**Webhooks arrive more than once.** Razorpay retries until it gets a 2xx, so the same
`subscription.charged` will be delivered again after any blip. Each event id is recorded and
a repeat is ignored, because processing one twice extends a paid-up period nobody paid for.

**Webhooks arrive out of order.** `subscription.activated` can land after the first
`subscription.charged`. Nothing here assumes a sequence: each handler sets the state it knows
about, and the expiry only ever moves FORWARD.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime

from config import settings
from services.entitlements import PLANS

logger = logging.getLogger("subscriptions")

# Which of our plans are recurring. Starter is a one-time purchase and stays on the Orders
# flow — putting a mandate on a product sold as "pay once" would be a different product.
RECURRING = {k for k, v in PLANS.items() if v["period_days"]}

# Razorpay subscription states in which the customer is genuinely entitled to the plan.
# `halted` is deliberately NOT one: a halted subscription is a card that failed and has
# stopped retrying, and continuing to serve a paid plan on it is giving the product away.
# The customer keeps access until `current_end` regardless, because that period was paid for.
LIVE_STATES = {"authenticated", "active", "pending"}
DEAD_STATES = {"halted", "cancelled", "completed", "expired"}


def enabled() -> bool:
    return bool(settings.payments_enabled)


def client():
    import razorpay
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


def _ts(value):
    """Razorpay sends unix seconds, and sends null for dates that do not apply yet."""
    if not value:
        return None
    try:
        return datetime.utcfromtimestamp(int(value))
    except (TypeError, ValueError, OSError):
        return None


# ── Razorpay plans ─────────────────────────────────────────────────────────────
# A Razorpay Plan is the price-and-period object a subscription is created against. Ours are
# created on first use and looked up by the `notes` we stamp on them, so there is no plan id
# to paste into a config file and no second place for the price to drift from entitlements.py.

def find_or_create_plan(our_plan: str) -> str:
    """The Razorpay plan id for one of our recurring plans, creating it if needed."""
    spec = PLANS[our_plan]
    if not spec["period_days"]:
        raise ValueError(f"{our_plan} is not a recurring plan")

    c = client()
    # Match on our own marker rather than on the name: a name is editable in their dashboard,
    # and a plan whose name someone tidied would be silently duplicated on the next call.
    try:
        existing = c.plan.all({"count": 100})
        for p in existing.get("items", []):
            notes = p.get("notes") or {}
            if notes.get("app_plan") == our_plan and int(
                    (p.get("item") or {}).get("amount", 0)) == int(spec["amount"] * 100):
                return p["id"]
    except Exception:
        logger.warning("subscriptions: could not list existing plans", exc_info=True)

    created = c.plan.create({
        "period": "monthly",
        "interval": 1,
        "item": {
            "name": f"{spec['label']} (monthly)",
            "amount": int(spec["amount"] * 100),
            "currency": "INR",
            "description": f"Finthara {spec['label']} plan, billed monthly",
        },
        "notes": {"app_plan": our_plan},
    })
    logger.info("subscriptions: created Razorpay plan %s for %s", created["id"], our_plan)
    return created["id"]


def create_subscription(user, our_plan: str, total_count: int = 120) -> dict:
    """Start a mandate for this user. Returns the raw Razorpay subscription.

    `total_count` is how many cycles the mandate covers — Razorpay requires a finite number,
    so it is set to ten years rather than to something the customer would hit and silently
    lose their plan over.
    """
    plan_id = find_or_create_plan(our_plan)
    sub = client().subscription.create({
        "plan_id": plan_id,
        "total_count": total_count,
        "quantity": 1,
        "customer_notify": 1,
        "notes": {"user_id": str(user.id), "app_plan": our_plan, "email": user.email or ""},
    })
    logger.info("subscriptions: created %s for user %s on %s", sub["id"], user.id, our_plan)
    return sub


# ── webhooks ───────────────────────────────────────────────────────────────────

def verify_webhook(body: bytes, signature: str) -> bool:
    """HMAC-SHA256 of the RAW body with the webhook secret.

    The raw bytes matter: re-serialising the parsed JSON changes key order and spacing, and
    the signature then never matches. This is the only thing standing between the endpoint and
    anyone on the internet posting "subscription.charged" for someone else's account, so it
    fails closed — no secret configured means no webhook is accepted.
    """
    secret = getattr(settings, "RAZORPAY_WEBHOOK_SECRET", "") or ""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _sub_entity(payload: dict) -> dict:
    return (((payload or {}).get("payload") or {}).get("subscription") or {}).get("entity") or {}


def apply_event(db, payload: dict) -> str:
    """Move our state to match the event. Returns a one-line note for the audit row.

    Every branch is written to be safe to run twice, because the caller cannot promise it will
    not be: the de-duplication by event id is the first line of defence, not the only one.
    """
    from models.subscription_model import Subscription
    from models.user_model import User

    event = (payload or {}).get("event") or ""
    ent = _sub_entity(payload)
    sub_id = ent.get("id")
    if not sub_id:
        return f"{event}: no subscription in the payload, ignored"

    sub = db.query(Subscription).filter(
        Subscription.razorpay_subscription_id == sub_id).first()
    if not sub:
        # A mandate we have no record of. Do NOT invent one: the only way to know which user
        # it belongs to is the notes we stamped at creation, and acting on a subscription we
        # did not create is how a webhook endpoint grants plans to strangers.
        user_id = (ent.get("notes") or {}).get("user_id")
        if not user_id or not str(user_id).isdigit():
            return f"{event}: subscription {sub_id} is unknown and carries no user, ignored"
        our_plan = (ent.get("notes") or {}).get("app_plan") or ""
        if our_plan not in RECURRING:
            return f"{event}: subscription {sub_id} names an unknown plan {our_plan!r}, ignored"
        sub = Subscription(user_id=int(user_id), plan=our_plan,
                           razorpay_plan_id=ent.get("plan_id") or "",
                           razorpay_subscription_id=sub_id)
        db.add(sub)
        logger.warning("subscriptions: recorded %s from a webhook — it was created outside "
                       "this server or its row was lost", sub_id)

    status = ent.get("status") or sub.status
    sub.status = status
    sub.current_start = _ts(ent.get("current_start")) or sub.current_start
    sub.current_end = _ts(ent.get("current_end")) or sub.current_end
    sub.charge_at = _ts(ent.get("charge_at")) or sub.charge_at
    if ent.get("paid_count") is not None:
        sub.paid_count = int(ent["paid_count"])
    if event == "subscription.cancelled":
        sub.cancelled_at = sub.cancelled_at or datetime.utcnow()

    user = db.get(User, sub.user_id)
    if not user:
        db.commit()
        return f"{event}: subscription {sub_id} has no user, state recorded only"

    if status in LIVE_STATES or event == "subscription.charged":
        user.plan = sub.plan
        # Only ever forward. Events arrive out of order, and an older event carrying an
        # earlier current_end must not claw back a period a later one already granted.
        if sub.current_end and (not user.plan_expires_at or sub.current_end > user.plan_expires_at):
            user.plan_expires_at = sub.current_end
        # Every charge is a sale, so every charge gets its own document. Keyed on the
        # cycle number inside the invoice service, so a webhook delivered twice for the
        # same cycle does not issue two.
        if event == "subscription.charged":
            try:
                from services import invoices as invoice_service
                from services.entitlements import plan_spec
                invoice_service.for_subscription_charge(
                    db, sub, plan_spec(sub.plan)["amount"])
            except Exception:
                logger.exception("subscriptions: invoice failed for %s cycle %s",
                                 sub_id, sub.paid_count)
        note = (f"{event}: {sub_id} {status}, user {user.id} on {sub.plan} "
                f"until {user.plan_expires_at}")
    elif status in DEAD_STATES:
        # The plan is NOT stripped here. The customer paid for the current period and keeps
        # it; effective_plan drops them to free by itself once current_end passes. Cancelling
        # someone's access the moment they cancel the renewal is taking back what they bought.
        note = (f"{event}: {sub_id} {status}, access left to run out at "
                f"{user.plan_expires_at}")
    else:
        note = f"{event}: {sub_id} {status}, no entitlement change"

    db.commit()
    logger.info("subscriptions: %s", note)
    return note


def record_and_apply(db, event_id: str, body: bytes, payload: dict) -> tuple[bool, str]:
    """(processed, note). Refuses to act twice on the same delivery."""
    from models.subscription_model import WebhookEvent

    if event_id:
        seen = db.query(WebhookEvent).filter(WebhookEvent.event_id == event_id).first()
        if seen:
            return False, f"duplicate delivery of {event_id}, ignored"

    row = WebhookEvent(event_id=event_id or f"(none):{datetime.utcnow().isoformat()}",
                       event=(payload or {}).get("event"),
                       payload=body.decode("utf-8", "replace")[:20000])
    db.add(row)
    db.commit()
    try:
        note = apply_event(db, payload)
        row.handled, row.note = True, note[:500]
    except Exception as exc:
        # The row stays, marked unhandled, with the reason. A webhook that failed silently is
        # a subscription that quietly stops renewing and nobody finds out until a customer
        # complains.
        row.handled, row.note = False, f"{type(exc).__name__}: {exc}"[:500]
        db.commit()
        raise
    db.commit()
    return True, note

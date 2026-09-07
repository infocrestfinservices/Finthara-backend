"""Razorpay checkout for the three pricing plans.

The one rule everything here is built around: **the price is decided by the server**. The
browser says which plan it wants, never what it costs. If the amount came from the client,
anyone could open the console and buy the ₹4,999 plan for ₹1 — and the payment would verify
perfectly, because Razorpay only signs what it was asked to charge.

The flow is the standard three steps:
  1. POST /payments/order   — we create a Razorpay order for the plan's server-side price
                              and record it as "created"
  2. the browser opens Razorpay's checkout with that order
  3. POST /payments/verify  — Razorpay hands back an order_id, payment_id and signature; we
                              recompute the signature with our SECRET and only then mark the
                              payment paid and move the user onto the plan.

Step 3 is the whole security of it. A client that simply POSTs "I paid" gets rejected,
because it cannot produce a signature without the key secret, which never leaves this file's
side of the wire.
"""
import hashlib
import hmac
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from dependencies import get_current_user
from models.payment_model import Payment
from models.subscription_model import Subscription
from models.user_model import User
from services.entitlements import (PLANS, PURCHASABLE, can_purchase, expiry_for,
                                   grant_plan)
from services import coupons as coupon_service
from services import subscriptions as subs
from services.email_service import send_plan_purchase_email

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/payments", tags=["Payments"])


def _send_purchase_email(user: User, payment: Payment, invoice) -> None:
    """Best-effort — see services/email_service.py. Never raised past this point: a mail
    provider outage must not turn an otherwise-successful, already-committed payment into an
    error response. `invoice` may be None (its own issuance can fail independently), in which
    case the payment id stands in so the email still has SOME reference number."""
    try:
        send_plan_purchase_email(
            user.email, user.full_name,
            invoice_number=(invoice.invoice_number if invoice else f"payment-{payment.id}"),
            plan_label=PLANS.get(payment.plan, {}).get("name", payment.plan),
            amount=float(payment.amount or 0), currency=payment.currency or "INR",
            discount=float(payment.discount or 0))
    except Exception:
        logger.exception("payments: purchase email could not be sent for payment %s",
                         payment.id)

# The price list lives on the SERVER, not in the frontend — the pricing page may show
# whatever it likes; what gets charged is this. It is imported rather than restated because
# the same table also decides what a plan ALLOWS (services/entitlements.py). Two copies of
# it would drift, and the first sign of that would be a customer paying for a plan and
# receiving a different one's limits.
PLANS = {k: {"name": v["label"], "amount": v["amount"], "period": v["period"]}
         for k, v in PURCHASABLE.items()}


class OrderRequest(BaseModel):
    plan: str
    # A code, and nothing else. What it is worth is decided here, against the coupon row —
    # a discount sent from the browser would be signed by Razorpay just as faithfully as a
    # real one, because their signature covers the amount they were asked to charge, not
    # whether that amount was right.
    coupon: str | None = None


class VerifyRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


def _client():
    if not settings.payments_enabled:
        raise HTTPException(status_code=503,
                            detail="Payments are not configured on this server.")
    import razorpay
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


@router.get("/config")
def payment_config():
    """What the checkout needs before it can open. The SECRET is never part of this."""
    return {
        "enabled": settings.payments_enabled,
        "key_id": settings.RAZORPAY_KEY_ID if settings.payments_enabled else "",
        "currency": "INR",
        "plans": [{"id": k, **v} for k, v in PLANS.items()],
    }


@router.post("/order")
def create_order(req: OrderRequest, current_user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    """Create a Razorpay order for a plan, at the price this server holds for it."""
    plan = PLANS.get((req.plan or "").strip().lower())
    if not plan:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {req.plan}")
    # Checked here rather than after payment: refusing a sale costs nothing, whereas taking
    # money for something that leaves someone with LESS than they had is the worst outcome a
    # checkout can produce — and it is exactly what happened on the first real payment, when
    # an account on Professional bought Starter and dropped to 3 reports and PDF only.
    allowed, why = can_purchase(db, current_user, req.plan)
    if not allowed:
        raise HTTPException(status_code=409, detail=why)

    amount = float(plan["amount"])
    discount = 0.0
    coupon = None
    if (req.coupon or "").strip():
        try:
            coupon, discount, amount = coupon_service.validate(
                db, req.coupon, req.plan, current_user)
        except coupon_service.CouponError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if amount < coupon_service.MIN_CHARGEABLE:
            # The code covers the whole price. Razorpay will not create an order under a
            # rupee, and asking a customer to pay ₹1 they were told they would not owe is
            # worse than granting it. Recorded as a fully-discounted payment so it appears in
            # the admin panel and in the coupon's redemption count like any other use.
            paid = Payment(user_id=current_user.id, plan=req.plan.lower(),
                           amount=0.0, amount_paise=0, currency="INR",
                           razorpay_order_id=f"free-{coupon.code}-{current_user.id}-"
                                             f"{int(datetime.utcnow().timestamp())}",
                           coupon_code=coupon.code, discount=discount,
                           status="paid", paid_at=datetime.utcnow())
            db.add(paid)
            grant_plan(current_user, req.plan.lower())
            db.commit()
            coupon_service.redeem(db, coupon, current_user, req.plan.lower(),
                                  float(plan["amount"]), discount, 0.0, paid.id)
            logger.info("payments: %s covered the full price of %s for user %s",
                        coupon.code, req.plan, current_user.id)
            # A ₹0 sale is still a sale — the customer needs a document for it same as a paid
            # one, and the coupon-covered branch was the one place that never issued one.
            invoice = None
            try:
                from services import invoices as invoice_service
                invoice = invoice_service.for_payment(db, paid)
            except Exception:
                logger.exception("payments: invoice could not be issued for payment %s",
                                 paid.id)
            _send_purchase_email(current_user, paid, invoice)
            return {"free": True, "plan": {"id": req.plan.lower(), **plan},
                    "discount": discount, "amount": 0,
                    "message": f"{coupon.code} covers the full price — your plan is active."}

    paise = int(round(amount * 100))               # Razorpay bills in paise, always
    try:
        order = _client().order.create({
            "amount": paise,
            "currency": "INR",
            "receipt": f"u{current_user.id}-{plan['name'].lower()}",
            "notes": {"user_id": str(current_user.id), "plan": req.plan},
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("payments: could not create an order for user %s", current_user.id)
        raise HTTPException(status_code=502, detail=f"Could not start the payment: {e}")

    db.add(Payment(user_id=current_user.id, plan=req.plan.lower(),
                   amount=amount, amount_paise=paise, currency="INR",
                   razorpay_order_id=order["id"],
                   coupon_code=(coupon.code if coupon else None), discount=discount,
                   status="created"))
    db.commit()
    logger.info("payments: order %s created for user %s (%s, INR %s)",
                order["id"], current_user.id, req.plan, plan["amount"])
    return {
        "order_id": order["id"],
        "amount": paise,
        "currency": "INR",
        "key_id": settings.RAZORPAY_KEY_ID,
        "plan": {"id": req.plan.lower(), **plan},
        "list_amount": float(plan["amount"]),
        "discount": discount,
        "coupon": (coupon.code if coupon else None),
        "prefill": {"name": current_user.full_name or "", "email": current_user.email},
    }


@router.post("/verify")
def verify_payment(req: VerifyRequest, current_user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    """Check Razorpay's signature, then record the payment and move the user's plan."""
    if not settings.payments_enabled:
        raise HTTPException(status_code=503, detail="Payments are not configured.")

    payment = db.query(Payment).filter(
        Payment.razorpay_order_id == req.razorpay_order_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail="That order was not found.")
    if payment.user_id != current_user.id:
        # The order belongs to someone else. 404 rather than 403 so this cannot be used to
        # confirm that another user's order exists.
        raise HTTPException(status_code=404, detail="That order was not found.")
    if payment.status == "paid":
        return {"status": "paid", "plan": payment.plan, "already": True}

    # HMAC-SHA256 of "<order_id>|<payment_id>" with the key secret. Razorpay computes the
    # same thing on their side; if ours does not match, the callback did not come from them.
    expected = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(),
        f"{req.razorpay_order_id}|{req.razorpay_payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, req.razorpay_signature or ""):
        payment.status = "failed"
        db.commit()
        logger.warning("payments: signature mismatch on order %s (user %s)",
                       req.razorpay_order_id, current_user.id)
        raise HTTPException(status_code=400, detail="Payment could not be verified.")

    payment.razorpay_payment_id = req.razorpay_payment_id
    payment.razorpay_signature = req.razorpay_signature
    payment.status = "paid"
    # The coupon is consumed HERE, not when the order was created. An abandoned checkout must
    # not use up a limited code, or a few people opening and closing the window would exhaust
    # a code nobody actually redeemed. The limits are re-checked because the last use may have
    # been taken by someone else in between.
    if payment.coupon_code:
        try:
            c, disc, final = coupon_service.validate(
                db, payment.coupon_code, payment.plan, current_user)
            coupon_service.redeem(db, c, current_user, payment.plan,
                                  final + disc, disc, final, payment.id)
        except coupon_service.CouponError as e:
            # The money is already taken and the plan is granted regardless — refusing the
            # customer their plan over a coupon counter would be the wrong way round. It is
            # logged so an over-redeemed code is visible rather than silent.
            logger.warning("payments: %s could not be redeemed for payment %s: %s",
                           payment.coupon_code, payment.id, e)
    payment.paid_at = datetime.utcnow()
    # grant_plan, not a bare assignment: buying a month EXTENDS whatever is left rather than
    # resetting to "30 days from now", which threw away time a customer had already paid for.
    grant_plan(current_user, payment.plan)
    db.commit()
    # The invoice is issued here, after the signature has verified and the money is real.
    # Never at order time: an abandoned checkout would leave a numbered document for a
    # payment that never happened, and the series is meant to be a record of actual sales.
    invoice = None
    try:
        from services import invoices as invoice_service
        invoice = invoice_service.for_payment(db, payment)
    except Exception:
        # The customer has paid and their plan is granted; a failed invoice must not turn
        # that into an error on their screen. It is logged and can be re-issued.
        logger.exception("payments: invoice could not be issued for payment %s", payment.id)
    _send_purchase_email(current_user, payment, invoice)

    logger.info("payments: order %s PAID by user %s — plan is now %s (expires %s)",
                req.razorpay_order_id, current_user.id, payment.plan,
                current_user.plan_expires_at or "never")
    return {"status": "paid", "plan": payment.plan, "amount": payment.amount,
            "payment_id": payment.razorpay_payment_id,
            "expires_at": (current_user.plan_expires_at.isoformat()
                           if current_user.plan_expires_at else None)}


class CouponPreview(BaseModel):
    code: str
    plan: str


@router.post("/coupon/preview")
def preview_coupon(req: CouponPreview, current_user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    """What a code is worth on a plan, before the customer commits to paying.

    Purely informational — the price charged is computed again from scratch when the order is
    created, so a stale or tampered preview cannot change what is billed.
    """
    try:
        coupon, discount, final = coupon_service.validate(db, req.code, req.plan, current_user)
    except coupon_service.CouponError as e:
        return {"valid": False, "message": str(e)}
    spec = PLANS[req.plan.strip().lower()]
    return {
        "valid": True, "code": coupon.code,
        "description": coupon.description,
        "list_amount": float(spec["amount"]),
        "discount": discount, "final_amount": final,
        "message": (f"{coupon.code} applied — ₹{discount:,.0f} off"
                    if discount else f"{coupon.code} applied"),
    }


@router.get("/me")
def my_payments(current_user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """This user's plan and their paid history — for a receipts / billing screen."""
    rows = (db.query(Payment)
            .filter(Payment.user_id == current_user.id, Payment.status == "paid")
            .order_by(Payment.paid_at.desc()).all())
    from services.entitlements import entitlements
    # Whether auto-pay is running, and when it next bills. Without this the billing screen
    # can only say "you are on Professional until the 10th" and not whether that date is a
    # renewal or the end of everything — which is the one thing a customer wants to know.
    live = (db.query(Subscription)
              .filter(Subscription.user_id == current_user.id)
              .order_by(Subscription.id.desc()).first())
    return {
        **entitlements(db, current_user),
        "subscription": ({
            "status": live.status,
            "plan": live.plan,
            "auto_pay": live.status in subs.LIVE_STATES and not live.cancel_at_cycle_end,
            "cancel_at_cycle_end": bool(live.cancel_at_cycle_end),
            "next_charge_at": live.charge_at.isoformat() if live.charge_at else None,
            "current_end": live.current_end.isoformat() if live.current_end else None,
            "paid_count": live.paid_count or 0,
        } if live else None),
        "payments": [{"plan": p.plan, "amount": p.amount, "currency": p.currency,
                      "payment_id": p.razorpay_payment_id,
                      "paid_at": p.paid_at.isoformat() if p.paid_at else None}
                     for p in rows],
    }


# ── auto-pay (Razorpay Subscriptions) ──────────────────────────────────────────
# The Orders flow above charges once. Professional and Enterprise are sold PER MONTH, and
# charging once for them granted the plan for ever. These three routes are the recurring
# path: take a mandate, let Razorpay debit it each cycle, and move the expiry to whatever
# date Razorpay says the customer is paid up to.


class SubscribeRequest(BaseModel):
    plan: str


@router.post("/subscribe")
def start_subscription(req: SubscribeRequest, current_user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    """Begin auto-pay for a monthly plan. Returns what the checkout needs."""
    plan = (req.plan or "").strip().lower()
    if plan not in subs.RECURRING:
        raise HTTPException(
            status_code=400,
            detail=f"{req.plan} is not a monthly plan. Starter is a one-time purchase.")
    if not subs.enabled():
        raise HTTPException(status_code=503, detail="Payments are not configured.")
    allowed, why = can_purchase(db, current_user, plan)
    if not allowed:
        raise HTTPException(status_code=409, detail=why)

    # One live mandate per user. Two would debit the card twice a month, and the second would
    # look exactly like the first in the dashboard.
    live = (db.query(Subscription)
              .filter(Subscription.user_id == current_user.id,
                      Subscription.status.in_(tuple(subs.LIVE_STATES)))
              .first())
    if live:
        raise HTTPException(
            status_code=409,
            detail=f"Auto-pay is already active on the {live.plan} plan. Cancel it before "
                   f"starting another.")

    try:
        sub = subs.create_subscription(current_user, plan)
    except Exception:
        # A 401 from the plans/subscriptions API means the Razorpay ACCOUNT does not have
        # Subscriptions enabled — the keys are fine, the feature is simply not switched on,
        # and activation is a request on the merchant's side that takes days.
        #
        # Refusing the sale for those days is the wrong answer: it makes the monthly plans
        # unbuyable, which is worse than the problem auto-pay was added to fix. The caller is
        # told to fall back to the ONE-TIME flow, which still charges the right amount and
        # still grants exactly 30 days (entitlements.expiry_for decides that, not this
        # endpoint) — the customer simply has to renew by hand until auto-pay is live.
        logger.exception("payments: could not create a subscription for user %s", current_user.id)
        raise HTTPException(
            status_code=503,
            detail={"message": "Auto-pay is not available yet on this payment account.",
                    "auto_pay_unavailable": True,
                    "fallback": "one_time"})

    row = Subscription(user_id=current_user.id, plan=plan,
                       razorpay_plan_id=sub.get("plan_id") or "",
                       razorpay_subscription_id=sub["id"],
                       status=sub.get("status") or "created")
    db.add(row)
    db.commit()
    return {
        "subscription_id": sub["id"],
        "key_id": settings.RAZORPAY_KEY_ID,
        "plan": {"id": plan, **PLANS[plan]},
        "short_url": sub.get("short_url"),
        "prefill": {"name": current_user.full_name or "", "email": current_user.email},
    }


@router.post("/subscription/cancel")
def cancel_subscription(at_cycle_end: bool = True,
                        current_user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """Stop auto-pay. By default the customer keeps the plan until the period they have
    already paid for ends — cancelling access the moment someone cancels the renewal is
    taking back what they bought."""
    row = (db.query(Subscription)
             .filter(Subscription.user_id == current_user.id,
                     Subscription.status.in_(tuple(subs.LIVE_STATES)))
             .order_by(Subscription.id.desc()).first())
    if not row:
        raise HTTPException(status_code=404, detail="There is no active auto-pay to cancel.")
    try:
        subs.client().subscription.cancel(row.razorpay_subscription_id,
                                          {"cancel_at_cycle_end": 1 if at_cycle_end else 0})
    except Exception:
        logger.exception("payments: cancel failed for %s", row.razorpay_subscription_id)
        raise HTTPException(status_code=502, detail="Could not cancel with the payment provider.")

    row.cancel_at_cycle_end = bool(at_cycle_end)
    if not at_cycle_end:
        row.status, row.cancelled_at = "cancelled", datetime.utcnow()
    db.commit()
    logger.info("payments: user %s cancelled %s (at_cycle_end=%s)",
                current_user.id, row.razorpay_subscription_id, at_cycle_end)
    return {"status": row.status, "cancel_at_cycle_end": row.cancel_at_cycle_end,
            "access_until": row.current_end.isoformat() if row.current_end else None}


@router.post("/webhook")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    """Where every recurring charge is actually learned about.

    Unauthenticated by nature — Razorpay calls it, not a logged-in browser — so the signature
    IS the authentication. The raw body is hashed, not the re-serialised JSON, because
    re-serialising changes key order and the signature would never match.

    Always answers 2xx once the delivery is recorded, including for events we do not act on.
    A non-2xx makes Razorpay retry, and retrying an event that will never be handled just
    fills the queue.
    """
    body = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")
    if not subs.verify_webhook(body, signature):
        logger.warning("payments: webhook rejected — bad or missing signature")
        raise HTTPException(status_code=400, detail="Invalid signature.")

    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Body is not JSON.")

    event_id = request.headers.get("x-razorpay-event-id", "")
    processed, note = subs.record_and_apply(db, event_id, body, payload)
    return {"ok": True, "processed": processed, "note": note}

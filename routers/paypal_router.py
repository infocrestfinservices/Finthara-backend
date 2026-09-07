"""PayPal checkout for the pricing plans — the same product payment_router.py sells through
Razorpay, offered through a second gateway for a buyer who would rather pay in USD via PayPal.

Same rule as the Razorpay flow: **the price is decided by the server.** The browser says
which plan it wants; the server looks up that plan's USD price (services/entitlements.py) and
creates the PayPal order against IT. Approval by the buyer is not payment — PayPal is only
asked to actually take the money at capture time, and a plan is granted only once PayPal's
capture comes back COMPLETED.

Two paths both call `_finalize_capture` and end at the same place:
  1. Browser callback (POST /paypal/verify) — the normal path, right after the buyer approves.
  2. Webhook (POST /paypal/webhook) — the safety net for a browser that closed, a network
     blip, or a frontend bug, and PayPal's own record of what actually happened either way.
Both are safe to run concurrently or twice: the Payment row is locked with SELECT ... FOR
UPDATE while its status is read and decided, and the PayPal capture call itself is idempotent
(a deterministic `capture-{order_id}` key), so whichever caller gets there first wins and the
other sees `status == "paid"` already and returns without charging anything twice.
"""
import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from dependencies import get_current_user
from models.payment_model import Payment
from models.subscription_model import WebhookEvent
from models.user_model import User
from services.entitlements import PLANS, can_purchase, grant_plan
from services import paypal_gateway as gw
from services.email_service import send_plan_purchase_email

logger = logging.getLogger("paypal")
router = APIRouter(prefix="/paypal", tags=["PayPal"])


@router.get("/config")
def paypal_config():
    return {
        "enabled": settings.paypal_enabled,
        "client_id": settings.PAYPAL_CLIENT_ID if settings.paypal_enabled else "",
        "currency": settings.PAYPAL_CURRENCY,
        "plans": [{"id": k, "name": v["label"], "amount": v["usd_amount"], "period": v["period"]}
                  for k, v in PLANS.items() if v["amount"] > 0],
    }


class OrderRequest(BaseModel):
    plan: str


@router.post("/order")
def create_order(req: OrderRequest, current_user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    if not settings.paypal_enabled:
        raise HTTPException(status_code=503, detail="PayPal is not configured on this server.")

    plan_key = (req.plan or "").strip().lower()
    spec = PLANS.get(plan_key)
    if not spec or spec["amount"] <= 0:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {req.plan}")
    allowed, why = can_purchase(db, current_user, plan_key)
    if not allowed:
        raise HTTPException(status_code=409, detail=why)

    amount = float(spec["usd_amount"])
    # Row written BEFORE PayPal is even called, the same way the Razorpay flow does it — an
    # abandoned checkout must still be findable afterwards, and it can only be found against
    # an id we recorded when we asked for it. payment.id doubles as PayPal's `custom_id`,
    # which is what lets the webhook find this row from a capture event that carries no
    # order id of its own.
    payment = Payment(user_id=current_user.id, gateway="paypal", plan=plan_key,
                      amount=amount, amount_paise=int(round(amount * 100)),
                      currency=settings.PAYPAL_CURRENCY, status="created")
    db.add(payment)
    db.commit()
    db.refresh(payment)

    try:
        order = gw.create_order(
            amount=f"{amount:.2f}", currency=settings.PAYPAL_CURRENCY,
            reference_id=f"payment-{payment.id}", custom_id=str(payment.id),
            idempotency_key=str(uuid.uuid4()))
    except gw.PayPalError as e:
        logger.exception("paypal: could not create an order for user %s (payment %s)",
                         current_user.id, payment.id)
        raise HTTPException(status_code=502, detail=f"Could not start the payment: {e}")

    payment.paypal_order_id = order["id"]
    db.commit()
    logger.info("paypal: order %s created for user %s (%s, %s %s)",
               order["id"], current_user.id, plan_key, settings.PAYPAL_CURRENCY, amount)
    return {
        "order_id": order["id"],
        "client_id": settings.PAYPAL_CLIENT_ID,
        "currency": settings.PAYPAL_CURRENCY,
        "amount": amount,
        "plan": {"id": plan_key, "name": spec["label"], "amount": amount},
    }


def _finalize_capture(db: Session, payment: Payment, order_id: str) -> str:
    """Capture `order_id` (idempotent — see module docstring) and, only once PayPal says
    COMPLETED, mark `payment` paid and grant the plan. Returns the outcome: "paid",
    "already_paid", "pending", or "failed". Caller holds the row lock (or doesn't need to —
    this re-checks payment.status itself, so it's safe to call from a lock-free path too, as
    long as no two callers can commit between this function's read and write, which the
    caller getting here via SELECT ... FOR UPDATE guarantees)."""
    if payment.status == "paid":
        return "already_paid"

    try:
        order = gw.capture_order(order_id)
    except gw.PayPalError:
        logger.exception("paypal: capture failed for order %s (payment %s)",
                         order_id, payment.id)
        payment.status = "failed"
        db.commit()
        return "failed"

    status = gw.capture_status(order)
    if status == "PENDING":
        # The money is not confirmed received. Left as "created" (not "failed") — a pending
        # capture can still complete, and a later capture/webhook call for the same order_id
        # will pick this row up again by paypal_order_id.
        logger.info("paypal: order %s capture is PENDING, not activating yet", order_id)
        return "pending"
    if status != "COMPLETED":
        logger.warning("paypal: order %s capture came back %r, marking failed",
                       order_id, status)
        payment.status = "failed"
        db.commit()
        return "failed"

    got = gw.captured_amount(order)
    if not got or float(got[0]) + 1e-6 < float(payment.amount) or got[1] != payment.currency:
        # PayPal captured LESS than we asked for, or in the wrong currency. Never seen in
        # normal operation — this is a defence against a tampered order id / a bug — but a
        # plan must never be granted for less than its price, so this fails closed rather
        # than trusting that COMPLETED alone means "the right amount arrived".
        logger.error("paypal: order %s captured %s, expected %s %s — refusing to activate",
                    order_id, got, payment.amount, payment.currency)
        payment.status = "failed"
        db.commit()
        return "failed"

    payment.paypal_capture_id = gw.capture_id(order)
    payment.status = "paid"
    payment.paid_at = datetime.utcnow()
    grant_plan(payment.user, payment.plan)
    db.commit()

    invoice = None
    try:
        from services import invoices as invoice_service
        invoice = invoice_service.for_payment(db, payment)
    except Exception:
        logger.exception("paypal: invoice could not be issued for payment %s", payment.id)

    try:
        send_plan_purchase_email(
            payment.user.email, payment.user.full_name,
            invoice_number=(invoice.invoice_number if invoice else f"payment-{payment.id}"),
            plan_label=PLANS.get(payment.plan, {}).get("label", payment.plan),
            amount=float(payment.amount or 0), currency=payment.currency or "USD",
            discount=float(payment.discount or 0))
    except Exception:
        logger.exception("paypal: purchase email could not be sent for payment %s", payment.id)

    logger.info("paypal: order %s PAID by user %s — plan is now %s (expires %s)",
               order_id, payment.user_id, payment.plan,
               payment.user.plan_expires_at or "never")
    return "paid"


class VerifyRequest(BaseModel):
    order_id: str


@router.post("/verify")
def verify_payment(req: VerifyRequest, current_user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    """Called by the frontend right after the buyer approves in the PayPal Buttons flow.
    Approval alone changes nothing here — this is what actually captures the money."""
    if not settings.paypal_enabled:
        raise HTTPException(status_code=503, detail="PayPal is not configured.")

    payment = (db.query(Payment)
                .filter(Payment.paypal_order_id == req.order_id)
                .with_for_update().first())
    if not payment:
        raise HTTPException(status_code=404, detail="That order was not found.")
    if payment.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="That order was not found.")

    outcome = _finalize_capture(db, payment, req.order_id)
    if outcome in ("paid", "already_paid"):
        return {"status": "paid", "plan": payment.plan, "amount": payment.amount,
               "already": outcome == "already_paid",
               "expires_at": (payment.user.plan_expires_at.isoformat()
                              if payment.user.plan_expires_at else None)}
    if outcome == "pending":
        return {"status": "pending", "plan": payment.plan}
    raise HTTPException(status_code=400, detail="Payment could not be completed.")


# ── Webhook — the safety net ─────────────────────────────────────────────────────────────

def _find_payment(db: Session, *, order_id: str | None, custom_id: str | None) -> Payment | None:
    q = db.query(Payment).filter(Payment.gateway == "paypal")
    if custom_id and custom_id.isdigit():
        # The most reliable path — we stamped this at order creation, and it survives every
        # event type PayPal sends, unlike order_id which not every capture event carries.
        row = q.filter(Payment.id == int(custom_id)).with_for_update().first()
        if row:
            return row
    if order_id:
        return q.filter(Payment.paypal_order_id == order_id).with_for_update().first()
    return None


def _resource_ids(resource: dict) -> tuple[str | None, str | None]:
    """(order_id, custom_id) out of a webhook resource, whose shape differs by event type:
    an ORDER.APPROVED resource IS the order (custom_id nested a level down, in its own
    purchase_units); a CAPTURE.* resource IS a capture (its own custom_id, and the order id
    only reachable via supplementary_data)."""
    if resource.get("intent") == "CAPTURE" and "purchase_units" in resource:
        order_id = resource.get("id")
        custom_id = ((resource.get("purchase_units") or [{}])[0]).get("custom_id")
        return order_id, custom_id
    order_id = (((resource.get("supplementary_data") or {})
                .get("related_ids") or {}).get("order_id"))
    return order_id, resource.get("custom_id")


@router.post("/webhook")
async def paypal_webhook(request: Request, db: Session = Depends(get_db)):
    """PayPal's delivery, independent of whatever the browser did or didn't manage to tell
    us. Unauthenticated by nature (PayPal cannot hold our login) — the signature IS the
    authentication, verified against the RAW body (see services/paypal_gateway.py).

    Response contract: 2xx once the delivery is recorded (PayPal stops retrying, including
    for events we don't act on), 4xx for a bad/unverifiable delivery (terminal — retrying a
    forged signature is pointless), 5xx only if something on our side genuinely failed and a
    retry might succeed.
    """
    body = await request.body()
    if not gw.verify_webhook_signature(headers=request.headers, raw_body=body,
                                       webhook_id=settings.PAYPAL_WEBHOOK_ID):
        logger.warning("paypal: webhook rejected — signature did not verify")
        raise HTTPException(status_code=400, detail="Invalid signature.")

    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Body is not JSON.")

    event_id = payload.get("id") or ""
    event_type = payload.get("event_type") or ""

    if event_id:
        seen = db.query(WebhookEvent).filter(
            WebhookEvent.gateway == "paypal", WebhookEvent.event_id == event_id).first()
        if seen:
            return {"ok": True, "processed": False, "note": "duplicate delivery, ignored"}

    row = WebhookEvent(gateway="paypal",
                       event_id=event_id or f"(none):{datetime.utcnow().isoformat()}",
                       event=event_type, payload=body.decode("utf-8", "replace")[:20000])
    db.add(row)
    db.commit()

    try:
        note = _apply_webhook_event(db, event_type, payload)
        row.handled, row.note = True, note[:500]
    except Exception as exc:
        row.handled, row.note = False, f"{type(exc).__name__}: {exc}"[:500]
        db.commit()
        raise
    db.commit()
    return {"ok": True, "processed": True, "note": note}


def _apply_webhook_event(db: Session, event_type: str, payload: dict) -> str:
    resource = payload.get("resource") or {}
    order_id, custom_id = _resource_ids(resource)

    if event_type in ("CHECKOUT.ORDER.APPROVED", "PAYMENT.CAPTURE.COMPLETED"):
        payment = _find_payment(db, order_id=order_id, custom_id=custom_id)
        if not payment:
            return f"{event_type}: no matching payment (order={order_id}, custom={custom_id})"
        target_order_id = payment.paypal_order_id or order_id
        if not target_order_id:
            return f"{event_type}: payment {payment.id} has no order id to capture"
        outcome = _finalize_capture(db, payment, target_order_id)
        return f"{event_type}: payment {payment.id} -> {outcome}"

    if event_type in ("PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.DECLINED"):
        payment = _find_payment(db, order_id=order_id, custom_id=custom_id)
        if not payment:
            return f"{event_type}: no matching payment (order={order_id}, custom={custom_id})"
        if payment.status != "paid":
            payment.status = "failed"
            db.commit()
        return f"{event_type}: payment {payment.id} marked failed"

    if event_type in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED"):
        # Logged, not acted on. Whether a refund should pull back access is a business call —
        # see the module docstring — and this endpoint's job is to record what PayPal says
        # happened, not to make that call unilaterally.
        payment = _find_payment(db, order_id=order_id, custom_id=custom_id)
        logger.warning("paypal: %s for payment %s (order=%s) — access NOT auto-revoked",
                       event_type, payment.id if payment else "?", order_id)
        return f"{event_type}: logged only, no entitlement change"

    return f"{event_type}: no handler, recorded only"

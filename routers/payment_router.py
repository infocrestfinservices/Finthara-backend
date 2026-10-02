"""Cashfree checkout for the pricing plans (Entrepreneur one-time, Consultant & CA monthly /
yearly — all sold as single payments; nothing here renews automatically).

The one rule everything here is built around: **the price is decided by the server**. The
browser says which plan it wants, never what it costs. The amount comes from
services/entitlements.py (less any coupon, also computed here), and a payment only counts if
Cashfree reports the order PAID for exactly that amount in INR.

The flow:
  1. POST /payments/order   — we record a Payment row, create a Cashfree order for the
                              server-side price and return its payment_session_id
  2. the browser opens cashfree.js checkout with that session
  3. POST /payments/verify  — we ASK CASHFREE what happened to the order (never trusting the
                              browser's say-so) and only on PAID + matching amount mark the
                              payment paid and grant the plan.
  4. POST /payments/webhook — Cashfree's server-to-server notice, signature-checked; the
                              safety net for a browser that closed before step 3. It runs the
                              same finalisation, which is safe to run twice.
"""
import hashlib
import json
import logging
import re
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
from services.entitlements import (PURCHASABLE, REGEN_PRICE, REGEN_PRODUCT, can_purchase,
                                   grant_plan)
from services import cashfree_gateway as cf
from services import gst
from services import coupons as coupon_service
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
            plan_label=(PLANS.get(payment.plan, {}).get("name")
                        or ("Report regeneration" if payment.plan == REGEN_PRODUCT
                            else payment.plan)),
            amount=float(payment.amount or 0), currency=payment.currency or "INR",
            discount=float(payment.discount or 0))
    except Exception:
        logger.exception("payments: purchase email could not be sent for payment %s",
                         payment.id)


def _issue_invoice_and_email(db: Session, user: User, payment: Payment) -> None:
    """The invoice is issued only once the money is real — never at order time, or an
    abandoned checkout would leave a numbered document for a sale that never happened."""
    invoice = None
    try:
        from services import invoices as invoice_service
        invoice = invoice_service.for_payment(db, payment)
    except Exception:
        # The customer has paid and their plan is granted; a failed invoice must not turn
        # that into an error on their screen. It is logged and can be re-issued.
        logger.exception("payments: invoice could not be issued for payment %s", payment.id)
    _send_purchase_email(user, payment, invoice)


# The price list lives on the SERVER, not in the frontend — the pricing page may show
# whatever it likes; what gets charged is this. Imported from entitlements rather than
# restated, because the same table decides what a plan ALLOWS.
PLANS = {k: {"name": v["label"], "amount": v["amount"], "period": v["period"]}
         for k, v in PURCHASABLE.items()}


class OrderRequest(BaseModel):
    plan: str
    # A code, and nothing else. What it is worth is decided here, against the coupon row.
    coupon: str | None = None
    # Cashfree requires a phone number. Sent only when the account has none on file; it is
    # then saved to the profile so it is not asked for again.
    phone: str | None = None
    # GST billing details (used only once GST applies). `state` is a GST state code or name
    # and decides CGST+SGST vs IGST; a business GSTIN overrides it with its own state.
    state: str | None = None
    gstin: str | None = None
    company: str | None = None
    # For plan="regeneration": the report the extra regeneration is bought for.
    project_id: int | None = None


def _regeneration_project(db: Session, user: User, project_id: int | None):
    """The report an extra regeneration is being bought for — the buyer must be able to
    regenerate it (its owner, or an editor on the owner's team), and it must have a report."""
    from models.project_model import Project
    from services.roles import role_in_company, meets
    project = db.query(Project).filter(Project.id == project_id).first() if project_id else None
    if not project:
        raise HTTPException(status_code=404, detail="Report not found.")
    if project.user_id != user.id:
        role = (role_in_company(db, owner_user_id=project.user_id, user_id=user.id)
                if project.user_id is not None else None)
        if not (role and meets(role, "editor")):
            raise HTTPException(status_code=404, detail="Report not found.")
    if project.report is None:
        raise HTTPException(status_code=400, detail="Generate the report first — "
                            "a regeneration is bought for an existing report.")
    return project


class VerifyRequest(BaseModel):
    order_id: str


def _clean_phone(raw: str | None) -> str | None:
    """A 10-digit Indian mobile number, or None. Accepts +91 / 0 prefixes and spacing."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if re.fullmatch(r"[6-9]\d{9}", digits) else None


def _return_url(order_id: str, regen_project=None) -> str | None:
    """Where Cashfree sends the browser back to for payment methods that leave the page
    (some netbanking / UPI flows). The pricing page picks up ?cf_order= and verifies it.
    Production Cashfree accepts only https, so a non-https FRONTEND_URL there sends none —
    the modal checkout and the webhook still complete the payment without it."""
    base = (settings.FRONTEND_URL or "").rstrip("/")
    if not base or (settings.cashfree_production and not base.startswith("https://")):
        return None
    # A regeneration bought from the report screen returns to that report.
    page = f"/report/{regen_project.id}" if regen_project else "/pricing"
    return f"{base}{page}?cf_order={order_id}"


@router.get("/config")
def payment_config():
    """What the checkout needs before it can open. The secret key is never part of this."""
    return {
        "enabled": cf.enabled(),
        "gateway": "cashfree",
        "mode": cf.mode(),
        "currency": "INR",
        "plans": [{"id": k, **v} for k, v in PLANS.items()],
        # Prices above are GST-exclusive. When GST applies, checkout adds `rate` on top and
        # asks for the customer's state (from this list).
        "gst": {"registered": gst.registered(), "rate": gst.rate(),
                "company_state": gst.company_state(),
                "states": [{"code": c, "name": n} for c, n in gst.STATES.items()]},
    }


@router.post("/order")
def create_order(req: OrderRequest, current_user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    """Create a Cashfree order for a plan, at the price this server holds for it."""
    plan_key = (req.plan or "").strip().lower()
    regen_project = None
    if plan_key == REGEN_PRODUCT:
        # One extra regeneration for one report — not a plan, and never discounted.
        regen_project = _regeneration_project(db, current_user, req.project_id)
        if (req.coupon or "").strip():
            raise HTTPException(status_code=400, detail="Coupons do not apply to regenerations.")
        plan = {"name": "Report regeneration", "amount": REGEN_PRICE, "period": "one_time"}
    else:
        plan = PLANS.get(plan_key)
        if not plan:
            raise HTTPException(status_code=400, detail=f"Unknown plan: {req.plan}")
        # Refused BEFORE any money moves: taking money for something that leaves someone
        # with LESS than they had is the worst outcome a checkout can produce.
        allowed, why = can_purchase(db, current_user, plan_key)
        if not allowed:
            raise HTTPException(status_code=409, detail=why)

    amount = float(plan["amount"])
    discount = 0.0
    coupon = None
    if (req.coupon or "").strip():
        try:
            coupon, discount, amount = coupon_service.validate(
                db, req.coupon, plan_key, current_user)
        except coupon_service.CouponError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if amount < coupon_service.MIN_CHARGEABLE:
            # The code covers the whole price. A gateway will not create an order under a
            # rupee, and asking a customer to pay ₹1 they were told they would not owe is
            # worse than granting it. Recorded as a fully-discounted payment so it appears in
            # the admin panel and in the coupon's redemption count like any other use.
            paid = Payment(user_id=current_user.id, gateway="cashfree", plan=plan_key,
                           amount=0.0, amount_paise=0, currency="INR",
                           cashfree_order_id=f"free-{coupon.code}-{current_user.id}-"
                                             f"{int(datetime.utcnow().timestamp())}",
                           coupon_code=coupon.code, discount=discount,
                           status="paid", paid_at=datetime.utcnow())
            db.add(paid)
            grant_plan(current_user, plan_key)
            db.commit()
            coupon_service.redeem(db, coupon, current_user, plan_key,
                                  float(plan["amount"]), discount, 0.0, paid.id)
            logger.info("payments: %s covered the full price of %s for user %s",
                        coupon.code, plan_key, current_user.id)
            _issue_invoice_and_email(db, current_user, paid)
            return {"free": True, "plan": {"id": plan_key, **plan},
                    "discount": discount, "amount": 0,
                    "message": f"{coupon.code} covers the full price — your plan is active."}

    if not cf.enabled():
        # Not 503: App Platform's edge replaces a 503's body with its own error page.
        raise HTTPException(status_code=409, detail="Payments are not configured on this server.")

    # GST on top of the (coupon-reduced) plan price. The customer's state decides the split.
    customer_state = customer_gstin = customer_company = None
    if gst.registered():
        if (req.gstin or "").strip():
            customer_gstin = gst.clean_gstin(req.gstin)
            if not customer_gstin:
                raise HTTPException(status_code=400, detail="That GSTIN is not valid. "
                                    "It should be 15 characters, e.g. 23ABCDE1234F1Z5.")
            customer_state = customer_gstin[:2]      # a registered business is placed by it
            customer_company = (req.company or "").strip()[:150] or current_user.billing_company
        else:
            customer_state = gst.state_code(req.state) or gst.state_code(current_user.billing_state)
        if not customer_state:
            raise HTTPException(status_code=400, detail={
                "message": "Please choose your state — it decides how GST is applied.",
                "state_required": True})
        current_user.billing_state = customer_state
        if customer_gstin:
            current_user.billing_gstin = customer_gstin
            current_user.billing_company = customer_company
    tax = gst.compute(amount, customer_state)
    taxable = amount
    amount = tax["total"]

    phone = _clean_phone(current_user.phone) or _clean_phone(req.phone)
    if not phone:
        # The frontend shows a phone field on exactly this answer and sends the order again.
        raise HTTPException(status_code=400, detail={
            "message": "Please enter a 10-digit mobile number to continue to payment.",
            "phone_required": True})
    if not _clean_phone(current_user.phone):
        current_user.phone = phone

    # Row written BEFORE Cashfree is called, so an abandoned or failed attempt is still
    # findable; its id makes our order id unique.
    payment = Payment(user_id=current_user.id, gateway="cashfree", plan=plan_key,
                      amount=amount, amount_paise=int(round(amount * 100)), currency="INR",
                      coupon_code=(coupon.code if coupon else None), discount=discount,
                      taxable_amount=taxable, tax_rate=tax["tax_rate"], cgst=tax["cgst"],
                      sgst=tax["sgst"], igst=tax["igst"], customer_state=customer_state,
                      customer_gstin=customer_gstin, customer_company=customer_company,
                      project_id=(regen_project.id if regen_project else None),
                      status="created")
    db.add(payment)
    db.commit()
    db.refresh(payment)
    order_id = f"fin_{payment.id}_{int(datetime.utcnow().timestamp())}"
    payment.cashfree_order_id = order_id
    db.commit()

    try:
        order = cf.create_order(
            order_id=order_id, amount=amount,
            customer_id=f"user_{current_user.id}",
            customer_email=current_user.email, customer_phone=phone,
            customer_name=current_user.full_name or "",
            return_url=_return_url(order_id, regen_project), note=plan["name"])
    except cf.CashfreeError as e:
        payment.status = "failed"
        db.commit()
        raise HTTPException(status_code=409, detail=f"Could not start the payment: {e}")

    logger.info("payments: cashfree order %s created for user %s (%s, INR %s)",
                order_id, current_user.id, plan_key, amount)
    return {
        "order_id": order_id,
        "payment_session_id": order["payment_session_id"],
        "mode": cf.mode(),
        "amount": amount,
        "currency": "INR",
        "plan": {"id": plan_key, **plan},
        "list_amount": float(plan["amount"]),
        "discount": discount,
        "coupon": (coupon.code if coupon else None),
        "tax": tax,
    }


def _finalize(db: Session, payment: Payment) -> str:
    """Ask Cashfree what happened to this payment's order and act on it. Returns "paid",
    "already_paid", "pending" or "failed".

    The caller holds the row lock (SELECT ... FOR UPDATE), so the browser's verify call and
    the webhook cannot both grant the same payment; whichever runs second sees "paid".
    """
    if payment.status == "paid":
        return "already_paid"
    order_id = payment.cashfree_order_id
    try:
        order = cf.fetch_order(order_id)
    except cf.CashfreeError:
        return "pending"                    # could not ask — do not guess either way

    status = order["status"]
    if status in ("EXPIRED", "TERMINATED", "TERMINATION_REQUESTED"):
        payment.status = "failed"
        db.commit()
        return "failed"
    if status != "PAID":
        return "pending"                    # ACTIVE: not paid (yet) — a retry may still pay

    expected = round(float(payment.amount or 0), 2)
    if abs(order["amount"] - expected) > 0.009 or order["currency"] != "INR":
        # Never seen in normal operation — a defence against a tampered order or a bug. A
        # plan is never granted for less than its price.
        logger.error("payments: order %s is PAID for %s %s but %s INR was expected — "
                     "refusing to activate", order_id, order["amount"], order["currency"],
                     expected)
        payment.status = "failed"
        db.commit()
        return "failed"

    try:
        settled = cf.successful_payment(order_id)
    except cf.CashfreeError:
        settled = None
    user = payment.user
    payment.cashfree_payment_id = (settled or {}).get("cf_payment_id") or None
    payment.status = "paid"
    payment.paid_at = datetime.utcnow()
    # The coupon is consumed HERE, not when the order was created — an abandoned checkout
    # must not use up a limited code. Limits are re-checked because the last use may have
    # been taken by someone else in between.
    if payment.coupon_code:
        try:
            c, disc, final = coupon_service.validate(db, payment.coupon_code, payment.plan, user)
            coupon_service.redeem(db, c, user, payment.plan, final + disc, disc, final,
                                  payment.id)
        except coupon_service.CouponError as e:
            # The money is taken and the plan is granted regardless; logged so an
            # over-redeemed code is visible rather than silent.
            logger.warning("payments: %s could not be redeemed for payment %s: %s",
                           payment.coupon_code, payment.id, e)
    # grant_plan EXTENDS what is left rather than resetting it, and a top-up only adds a
    # report credit — see services/entitlements.py.
    if payment.plan == REGEN_PRODUCT:
        # One extra regeneration for the report it was bought for.
        from models.project_model import Project
        project = db.query(Project).filter(Project.id == payment.project_id).first()
        if project is not None:
            project.regeneration_credits = (project.regeneration_credits or 0) + 1
    else:
        grant_plan(user, payment.plan)
    db.commit()
    _issue_invoice_and_email(db, user, payment)
    logger.info("payments: order %s PAID by user %s — plan is now %s (expires %s)",
                order_id, user.id, user.plan, user.plan_expires_at or "never")
    return "paid"


@router.post("/verify")
def verify_payment(req: VerifyRequest, current_user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    """Called by the browser once the Cashfree checkout closes (or on the return URL).
    Nothing the browser says is trusted: the order's status is fetched from Cashfree."""
    if not cf.enabled():
        raise HTTPException(status_code=409, detail="Payments are not configured.")
    payment = (db.query(Payment)
                 .filter(Payment.cashfree_order_id == req.order_id)
                 .with_for_update().first())
    if not payment or payment.user_id != current_user.id:
        # 404 rather than 403 so this cannot confirm that another user's order exists.
        raise HTTPException(status_code=404, detail="That order was not found.")

    outcome = _finalize(db, payment)
    if outcome in ("paid", "already_paid"):
        db.refresh(current_user)
        return {"status": "paid", "plan": payment.plan, "amount": payment.amount,
                "already": outcome == "already_paid",
                "payment_id": payment.cashfree_payment_id,
                "expires_at": (current_user.plan_expires_at.isoformat()
                               if current_user.plan_expires_at else None)}
    if outcome == "pending":
        return {"status": "pending", "plan": payment.plan}
    return {"status": "failed", "plan": payment.plan}


@router.post("/webhook")
async def cashfree_webhook(request: Request, db: Session = Depends(get_db)):
    """Cashfree's delivery, independent of whatever the browser did.

    Unauthenticated by nature — the signature IS the authentication, checked against the RAW
    body. A bad or missing signature is a 400. Every genuine delivery gets a 2xx once it is
    recorded (Cashfree retries anything else), and a repeat of one already recorded is
    acknowledged and ignored.
    """
    body = await request.body()
    signature = request.headers.get("x-webhook-signature", "")
    timestamp = request.headers.get("x-webhook-timestamp", "")
    if not cf.verify_webhook(body, signature, timestamp):
        logger.warning("payments: cashfree webhook rejected — bad or missing signature")
        raise HTTPException(status_code=400, detail="Invalid signature.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Body is not JSON.")

    event_type = str(payload.get("type") or "")
    data = payload.get("data") or {}
    order_id = str((data.get("order") or {}).get("order_id") or "")
    cf_payment_id = str((data.get("payment") or {}).get("cf_payment_id") or "")
    pay_status = str((data.get("payment") or {}).get("payment_status") or "")
    # Cashfree sends no event id; a redelivery repeats the same event for the same payment,
    # so that combination identifies it.
    event_id = "cf_" + hashlib.sha256(
        f"{event_type}|{order_id}|{cf_payment_id}|{pay_status}".encode()).hexdigest()[:40]

    row = db.query(WebhookEvent).filter(WebhookEvent.gateway == "cashfree",
                                        WebhookEvent.event_id == event_id).first()
    if row and row.handled and "-> pending" not in (row.note or ""):
        return {"ok": True, "processed": False, "note": "duplicate delivery, ignored"}
    if not row:
        # A delivery that last time could not reach a conclusion (Cashfree's API was
        # unreachable, so "pending") is processed again rather than ignored.
        row = WebhookEvent(gateway="cashfree", event_id=event_id, event=event_type,
                           payload=body.decode("utf-8", "replace")[:20000])
        db.add(row)
        db.commit()

    try:
        if event_type == "PAYMENT_SUCCESS_WEBHOOK" and order_id:
            payment = (db.query(Payment)
                         .filter(Payment.cashfree_order_id == order_id)
                         .with_for_update().first())
            note = (f"{event_type}: payment {payment.id} -> {_finalize(db, payment)}"
                    if payment else f"{event_type}: no payment for order {order_id}")
        else:
            # A failed or abandoned ATTEMPT does not fail the order — the customer can retry
            # in the same checkout. Recorded only.
            note = f"{event_type or 'unknown'}: order {order_id or '?'} recorded only"
        row.handled, row.note = True, note[:500]
    except Exception as exc:
        row.handled, row.note = False, f"{type(exc).__name__}: {exc}"[:500]
        db.commit()
        raise
    db.commit()
    return {"ok": True, "processed": True, "note": note}


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
    spec = PLANS.get(req.plan.strip().lower())
    if not spec:
        return {"valid": False, "message": "That plan is not for sale."}
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
    return {
        **entitlements(db, current_user),
        # Remembered GST billing details, so checkout can pre-fill them.
        "billing": {"state": current_user.billing_state, "gstin": current_user.billing_gstin,
                    "company": current_user.billing_company},
        # Nothing renews automatically any more; kept so older clients reading it still work.
        "subscription": None,
        "payments": [{"plan": p.plan, "amount": p.amount, "currency": p.currency,
                      "gateway": p.gateway,
                      "payment_id": (p.cashfree_payment_id or p.paypal_capture_id
                                     or p.razorpay_payment_id),
                      "paid_at": p.paid_at.isoformat() if p.paid_at else None}
                     for p in rows],
    }

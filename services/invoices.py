"""Issuing invoices, and the arithmetic behind them.

Two rules shape everything here.

**The charge is the fact; the split is arithmetic.** `gross` is what the customer was actually
billed, and the taxable value and tax are derived FROM it — never the other way round. Compute
the tax first and add it, and the invoice stops agreeing with the bank statement the moment a
coupon or a rounding rupee is involved.

**An invoice is issued once.** Razorpay retries webhooks and a customer can double-click a
confirm button, so every entry point asks for the invoice belonging to that payment before
creating one. A second invoice for the same money is not a duplicate row, it is a second
document with its own number that a customer may act on.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from config import settings
from services.entitlements import plan_spec

logger = logging.getLogger("invoices")

# Indian financial years run April to March, so a payment in August 2026 belongs to FY 2026-27
# and its number reads INV-2026-…. Numbering by calendar year would split a financial year
# across two series, which is the one thing the series is not allowed to do.
FY_START_MONTH = 4


def financial_year(when: datetime) -> int:
    return when.year if when.month >= FY_START_MONTH else when.year - 1


def is_gst_registered() -> bool:
    return bool((settings.COMPANY_GSTIN or "").strip())


def document_title() -> str:
    """A business that is not registered for GST must not call its document a Tax Invoice —
    it issues a Bill of Supply. Driven by whether a GSTIN is configured, so registering later
    is one environment variable and not a code change."""
    return "TAX INVOICE" if is_gst_registered() else "BILL OF SUPPLY"


def split_amount(gross: float) -> dict:
    """Break a charged amount into what it is made of.

    With no GSTIN there is no tax to show: the whole amount is the value of the service, and
    the document carries a statement that the supplier is not registered.

    With a GSTIN, prices on the pricing page are treated as GST-INCLUSIVE, because ₹1,499 is
    what Razorpay actually charges. Adding tax on top would mean the customer is billed
    ₹1,769 for a plan advertised at ₹1,499.
    """
    gross = round(float(gross or 0), 2)
    if not is_gst_registered():
        return {"taxable_value": gross, "tax_rate": 0.0,
                "cgst": 0.0, "sgst": 0.0, "igst": 0.0, "total": gross}

    rate = float(settings.GST_RATE or 0)
    taxable = round(gross / (1 + rate), 2)
    tax = round(gross - taxable, 2)
    # Without the customer's state we cannot know whether the supply is intra-state, so it is
    # treated as intra-state and split CGST/SGST — the common case for a domestic B2C sale.
    # Collecting a billing state is what would let this be decided rather than assumed.
    half = round(tax / 2, 2)
    return {"taxable_value": taxable, "tax_rate": rate,
            "cgst": half, "sgst": round(tax - half, 2), "igst": 0.0, "total": gross}


def _next_number(db, when: datetime) -> str:
    from models.invoice_model import Invoice
    fy = financial_year(when)
    prefix = f"INV-{fy}-"
    last = (db.query(Invoice.invoice_number)
              .filter(Invoice.invoice_number.like(f"{prefix}%"))
              .order_by(Invoice.invoice_number.desc()).first())
    n = 1
    if last:
        try:
            n = int(last[0].rsplit("-", 1)[1]) + 1
        except (ValueError, IndexError):
            n = db.query(Invoice).filter(Invoice.invoice_number.like(f"{prefix}%")).count() + 1
    return f"{prefix}{n:04d}"


def _describe(plan: str, period_start, period_end) -> str:
    spec = plan_spec(plan)
    line = f"1x {spec['label']} Plan"
    if spec["period_days"]:
        line += " — Monthly Subscription"
        if period_start and period_end:
            line += (f", {period_start.strftime('%d %b %Y')} to "
                     f"{period_end.strftime('%d %b %Y')}")
    else:
        line += " — One-time purchase"
    return line


def for_payment(db, payment):
    """The invoice for a payment, creating it the first time. Returns None if there is
    nothing to invoice."""
    from models.invoice_model import Invoice
    from models.user_model import User

    if not payment or payment.status != "paid":
        return None
    existing = db.query(Invoice).filter(Invoice.payment_id == payment.id).first()
    if existing:
        return existing

    user = db.get(User, payment.user_id)
    issued = payment.paid_at or datetime.utcnow()
    period_end = None
    spec = plan_spec(payment.plan)
    if spec["period_days"]:
        from datetime import timedelta
        period_end = issued + timedelta(days=spec["period_days"])

    return _create(db, user=user, payment=payment, subscription=None,
                   plan=payment.plan, gross=float(payment.amount or 0),
                   discount=float(payment.discount or 0),
                   coupon_code=payment.coupon_code,
                   issued_at=issued, period_start=issued, period_end=period_end,
                   currency=payment.currency or "INR")


def for_subscription_charge(db, subscription, amount: float):
    """The invoice for one renewal. Keyed on the subscription's paid_count so a webhook
    delivered twice does not issue a second document for the same cycle."""
    from models.invoice_model import Invoice
    from models.user_model import User

    cycle = subscription.paid_count or 1
    tag = f"cycle:{cycle}"
    already = (db.query(Invoice)
                 .filter(Invoice.subscription_id == subscription.id,
                         Invoice.description.like(f"%{tag}%")).first())
    if already:
        return already

    user = db.get(User, subscription.user_id)
    issued = subscription.current_start or datetime.utcnow()
    return _create(db, user=user, payment=None, subscription=subscription,
                   plan=subscription.plan, gross=float(amount or 0), discount=0.0,
                   coupon_code=None, issued_at=issued,
                   period_start=subscription.current_start,
                   period_end=subscription.current_end,
                   description_suffix=f"  [{tag}]")


def _create(db, *, user, payment, subscription, plan, gross, discount, coupon_code,
            issued_at, period_start, period_end, description_suffix="", currency="INR"):
    from models.invoice_model import Invoice

    money = split_amount(gross)
    for attempt in range(5):
        inv = Invoice(
            invoice_number=_next_number(db, issued_at),
            user_id=user.id if user else (payment.user_id if payment else subscription.user_id),
            payment_id=payment.id if payment else None,
            subscription_id=subscription.id if subscription else None,
            customer_name=getattr(user, "full_name", None),
            customer_email=getattr(user, "email", "") or "",
            supplier_name=settings.COMPANY_NAME,
            supplier_address=settings.COMPANY_ADDRESS or None,
            supplier_email=settings.COMPANY_EMAIL or None,
            supplier_gstin=(settings.COMPANY_GSTIN or None),
            plan=plan,
            description=_describe(plan, period_start, period_end) + description_suffix,
            period_start=period_start, period_end=period_end,
            sac_code=(settings.SAC_CODE if is_gst_registered() else None),
            currency=currency,
            gross=round(float(gross), 2), discount=round(float(discount or 0), 2),
            coupon_code=coupon_code,
            amount_paid=round(float(gross), 2), amount_due=0.0,
            place_of_supply=(settings.COMPANY_STATE or None),
            issued_at=issued_at, status="paid",
            **money,
        )
        db.add(inv)
        try:
            db.commit()
            db.refresh(inv)
            logger.info("invoices: issued %s to %s for %s (Rs %.2f)",
                        inv.invoice_number, inv.customer_email, plan, inv.gross)
            return inv
        except IntegrityError:
            # Two payments landed together and both computed the same next number. The unique
            # constraint is what caught it; roll back and take the next one. This is why the
            # number is unique in the DATABASE and not merely in this function.
            db.rollback()
            if attempt == 4:
                logger.exception("invoices: could not allocate a number after 5 tries")
                raise
    return None

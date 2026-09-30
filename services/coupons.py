"""Discount codes, priced on the server.

The rule the whole module is built around is the same one the payment flow already follows:
**the browser says which code it wants to use, never what it is worth.** A discount computed
in the client and sent along would be charged by the gateway exactly as faithfully as a real one —
their signature covers the amount they were asked to charge, not whether that amount was
correct. So every price here is derived from the coupon row and the plan's own price, and the
request contributes nothing but a string.

Two rules that are easy to get wrong and expensive to get wrong:

**A coupon is consumed at VERIFICATION, not at checkout.** Opening a checkout and closing it
must not use up a limited code, or a few curious visitors exhaust a code nobody redeemed.

**Limits are re-checked at redemption.** Between previewing a code and paying with it, the
last use can be taken by someone else. Checking only at preview is how a "first 50 customers"
coupon ends up redeemed 60 times.
"""
from __future__ import annotations

import logging
from datetime import datetime

from services.entitlements import PLANS, PURCHASABLE

logger = logging.getLogger("coupons")

# Cashfree will not create an order below one rupee. A code that takes the price to zero is a
# legitimate thing to want (a free month for a partner), so it is handled by granting the plan
# outright instead of sending a ₹0 order that would be rejected.
MIN_CHARGEABLE = 1.0


class CouponError(Exception):
    """Rejected. The message is shown to the customer, so it says what to do next."""


def normalise(code: str) -> str:
    return (code or "").strip().upper()


def _plans_for(coupon) -> set[str]:
    raw = (coupon.applies_to or "").strip()
    if not raw:
        return set(PURCHASABLE)
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def compute_discount(coupon, amount: float) -> float:
    """What this coupon takes off `amount`, in rupees, rounded to paise.

    Never more than the amount itself: a ₹1,000 flat code against a ₹499 plan is a ₹499
    discount, not a ₹501 credit.
    """
    if coupon.kind == "flat":
        discount = float(coupon.value)
    else:
        discount = amount * (float(coupon.value) / 100.0)
    return round(min(max(discount, 0.0), amount), 2)


def find(db, code: str):
    from models.coupon_model import Coupon
    return db.query(Coupon).filter(Coupon.code == normalise(code)).first()


def validate(db, code: str, plan: str, user):
    """(coupon, discount, final_amount), or raise CouponError with a reason to show.

    Runs both at preview and again at redemption. Anything that can change in between — the
    clock, the redemption count, another customer taking the last one — is therefore checked
    twice, which is the point.
    """
    plan = (plan or "").strip().lower()
    spec = PLANS.get(plan)
    if not spec or spec["amount"] <= 0:
        raise CouponError("Coupons apply to paid plans only.")

    coupon = find(db, code)
    if not coupon:
        raise CouponError("That coupon code is not valid.")
    if not coupon.active:
        raise CouponError("That coupon is no longer active.")

    now = datetime.utcnow()
    if coupon.valid_from and now < coupon.valid_from:
        raise CouponError("That coupon is not active yet.")
    if coupon.valid_until and now > coupon.valid_until:
        raise CouponError("That coupon has expired.")

    if plan not in _plans_for(coupon):
        allowed = ", ".join(PLANS[p]["label"] for p in sorted(_plans_for(coupon)) if p in PLANS)
        raise CouponError(f"That coupon applies to {allowed or 'other plans'}.")

    if coupon.max_redemptions is not None and coupon.used_count >= coupon.max_redemptions:
        raise CouponError("That coupon has been fully redeemed.")

    if coupon.per_user_limit is not None and user is not None:
        from models.coupon_model import CouponRedemption
        mine = (db.query(CouponRedemption)
                  .filter(CouponRedemption.coupon_id == coupon.id,
                          CouponRedemption.user_id == user.id).count())
        if mine >= coupon.per_user_limit:
            raise CouponError("You have already used that coupon.")

    amount = float(spec["amount"])
    discount = compute_discount(coupon, amount)
    final = round(amount - discount, 2)
    return coupon, discount, final


def redeem(db, coupon, user, plan: str, original: float, discount: float,
           final: float, payment_id=None):
    """Record the use and increment the counter. Called only after a payment is verified."""
    from models.coupon_model import CouponRedemption
    db.add(CouponRedemption(coupon_id=coupon.id, user_id=user.id, payment_id=payment_id,
                            plan=plan, original_amount=original, discount=discount,
                            final_amount=final))
    coupon.used_count = (coupon.used_count or 0) + 1
    db.commit()
    logger.info("coupons: %s redeemed by user %s on %s — Rs %.2f off (%s of %s uses)",
                coupon.code, user.id, plan, discount, coupon.used_count,
                coupon.max_redemptions if coupon.max_redemptions is not None else "unlimited")

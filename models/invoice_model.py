from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship
from datetime import datetime

from database import Base


class Invoice(Base):
    """A document the customer keeps.

    Almost every field here is a SNAPSHOT, and that is the point. An invoice records what was
    true on the day it was issued: if the customer later changes their name, or the company
    moves office or registers for GST, the document they already hold must not change with
    them. Reading these from `users` or from config at render time would quietly rewrite
    history every time something was edited — which is exactly what an invoice exists to
    prevent.

    Linked to a Payment (every sale now). `subscription_id` is only set on invoices issued
    for Razorpay auto-pay renewals before the move to Cashfree.
    """
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, index=True)
    # INV-2026-0001 — the financial year the issue date falls in, then a consecutive number
    # within it. Unique at the database level, which is what makes the numbering safe when
    # two payments land at the same moment.
    invoice_number = Column(String, nullable=False, unique=True, index=True)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=True, index=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=True, index=True)

    # ── the customer, as they were ────────────────────────────────────────────
    customer_name = Column(String, nullable=True)
    customer_email = Column(String, nullable=False)

    # ── the supplier, as they were ────────────────────────────────────────────
    supplier_name = Column(String, nullable=False)
    supplier_address = Column(String, nullable=True)
    supplier_email = Column(String, nullable=True)
    supplier_gstin = Column(String, nullable=True)     # empty => Bill of Supply

    # ── what was sold ─────────────────────────────────────────────────────────
    plan = Column(String, nullable=False)
    description = Column(String, nullable=False)
    period_start = Column(DateTime, nullable=True)
    period_end = Column(DateTime, nullable=True)
    sac_code = Column(String, nullable=True)

    # ── the money ─────────────────────────────────────────────────────────────
    currency = Column(String, default="INR")
    # `gross` is what the customer was actually charged. Everything else is derived from it,
    # because the charge is the fact and the split is the arithmetic — never the other way
    # round, or the invoice stops agreeing with the bank statement.
    gross = Column(Float, nullable=False)
    discount = Column(Float, default=0.0)
    coupon_code = Column(String, nullable=True)

    taxable_value = Column(Float, nullable=False)
    tax_rate = Column(Float, default=0.0)
    cgst = Column(Float, default=0.0)
    sgst = Column(Float, default=0.0)
    igst = Column(Float, default=0.0)
    total = Column(Float, nullable=False)
    amount_paid = Column(Float, nullable=False)
    amount_due = Column(Float, default=0.0)

    place_of_supply = Column(String, nullable=True)

    status = Column(String, default="paid")            # paid | cancelled
    issued_at = Column(DateTime, default=datetime.utcnow, index=True)

    user = relationship("User")

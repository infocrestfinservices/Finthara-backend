from sqlalchemy import Column, Integer, String, DateTime, Float, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime

from database import Base


class Payment(Base):
    """One row per attempt, written when the order is created and updated when it is paid.

    A row exists even for an abandoned checkout, which is deliberate: a payment that the
    gateway took but that never reached us has to be findable afterwards, and it can only
    be found against an order we recorded when we asked for it.
    """
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # "cashfree" | "paypal" — which provider this row belongs to, and therefore which of the
    # id columns below is populated. "razorpay" rows are history from before Cashfree.
    gateway = Column(String, nullable=False, default="cashfree")

    plan = Column(String, nullable=False)          # entrepreneur | consultant_monthly | consultant_yearly
    # In the unit `currency` names — rupees for INR, dollars for USD, not paise/cents.
    amount = Column(Float, nullable=False)
    # Minor units (paise for INR, cents for USD) — what the gateway itself was actually asked
    # to charge, kept alongside `amount` because rounding rupees/cents to 2dp and re-deriving
    # the minor-unit integer later can drift by a paisa/cent from what was really charged.
    amount_paise = Column(Integer, nullable=False)
    currency = Column(String, default="INR")

    # Our order id at Cashfree (also what the webhook and the return URL carry), and the id
    # of the payment that settled it. Nullable because a row belongs to one gateway only;
    # a unique constraint never treats two NULLs as equal, so that is fine.
    cashfree_order_id = Column(String, nullable=True, unique=True, index=True)
    cashfree_payment_id = Column(String, nullable=True, index=True)

    # Legacy — Razorpay rows from before the move to Cashfree. Kept so past payments and
    # the invoices that point at them still read correctly; nothing new is written here.
    razorpay_order_id = Column(String, nullable=True, unique=True, index=True)
    razorpay_payment_id = Column(String, nullable=True, index=True)
    razorpay_signature = Column(String, nullable=True)

    paypal_order_id = Column(String, nullable=True, unique=True, index=True)
    paypal_capture_id = Column(String, nullable=True, index=True)

    # What the customer typed and what it was worth, decided by the SERVER at order time.
    # Kept on the payment because a refund or a GST invoice has to answer "what did they
    # actually pay", and re-deriving it later would give the wrong answer the moment the
    # coupon is edited.
    coupon_code = Column(String, nullable=True)
    discount = Column(Float, default=0.0)

    # GST, fixed at order time. `amount` above is the TOTAL charged (taxable + GST); these
    # say what it is made of, so the invoice repeats the checkout exactly rather than
    # re-deriving it. NULL on rows from before GST-exclusive pricing.
    taxable_amount = Column(Float, nullable=True)
    tax_rate = Column(Float, nullable=True)
    cgst = Column(Float, nullable=True)
    sgst = Column(Float, nullable=True)
    igst = Column(Float, nullable=True)
    customer_state = Column(String, nullable=True)      # GST state code
    customer_gstin = Column(String, nullable=True)
    customer_company = Column(String, nullable=True)

    status = Column(String, default="created")     # created | paid | failed
    created_at = Column(DateTime, default=datetime.utcnow)
    paid_at = Column(DateTime, nullable=True)

    user = relationship("User")

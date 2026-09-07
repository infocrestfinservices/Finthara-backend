from sqlalchemy import Column, Integer, String, DateTime, Float, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime

from database import Base


class Payment(Base):
    """One row per attempt, written when the order is created and updated when it is paid.

    A row exists even for an abandoned checkout, which is deliberate: a payment that
    Razorpay took but that never reached us has to be findable afterwards, and it can only
    be found against an order we recorded when we asked for it.
    """
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # "razorpay" | "paypal" — which provider this row belongs to, and therefore which of the
    # id columns below is populated. Existing rows predate PayPal and default to razorpay.
    gateway = Column(String, nullable=False, default="razorpay")

    plan = Column(String, nullable=False)          # starter | professional | enterprise
    # In the unit `currency` names — rupees for INR, dollars for USD, not paise/cents.
    amount = Column(Float, nullable=False)
    # Minor units (paise for INR, cents for USD) — what the gateway itself was actually asked
    # to charge, kept alongside `amount` because rounding rupees/cents to 2dp and re-deriving
    # the minor-unit integer later can drift by a paisa/cent from what was really charged.
    amount_paise = Column(Integer, nullable=False)
    currency = Column(String, default="INR")

    # Nullable now that a row can belong to either gateway — a PayPal payment has no Razorpay
    # order id and vice versa. `unique=True` still holds with many NULLs sitting in the
    # column: a SQL unique constraint never considers NULL equal to another NULL, so every
    # PayPal row's NULL razorpay_order_id coexists fine — the constraint only ever fires
    # when two rows claim the SAME real order id.
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

    status = Column(String, default="created")     # created | paid | failed
    created_at = Column(DateTime, default=datetime.utcnow)
    paid_at = Column(DateTime, nullable=True)

    user = relationship("User")

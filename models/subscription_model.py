from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime

from database import Base


class Subscription(Base):
    """A recurring mandate at Razorpay, mirrored here.

    A Payment is one charge that either happened or did not. A subscription is a LIFECYCLE —
    created, authenticated, active, charged again next month, halted when a card fails,
    cancelled — and each of those arrives as a webhook, sometimes twice and sometimes out of
    order. It needs its own row.

    Razorpay is the authority on the dates. `current_end` is what THEY say the paid-up period
    runs to, and it is what the user's plan expiry is set from; computing "+30 days" locally
    would drift from the real billing cycle the first time a charge retried.
    """
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    plan = Column(String, nullable=False)                 # our key: professional | enterprise
    razorpay_plan_id = Column(String, nullable=False)
    razorpay_subscription_id = Column(String, nullable=False, unique=True, index=True)

    # Razorpay's own vocabulary, stored verbatim rather than mapped into ours: created,
    # authenticated, active, pending, halted, cancelled, completed, expired. Translating it
    # on the way in would lose the distinction between "halted" (a charge failed, may
    # recover) and "cancelled" (the user ended it), which are the two cases support cares
    # about most.
    status = Column(String, default="created", index=True)

    current_start = Column(DateTime, nullable=True)
    current_end = Column(DateTime, nullable=True)
    charge_at = Column(DateTime, nullable=True)
    paid_count = Column(Integer, default=0)

    # Cancelling at cycle end leaves the user paid up until current_end; this records that
    # the mandate will not renew, which is different from being cancelled already.
    cancel_at_cycle_end = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    cancelled_at = Column(DateTime, nullable=True)

    user = relationship("User")


class WebhookEvent(Base):
    """Every webhook Razorpay or PayPal has delivered, by their event id.

    Both providers retry a webhook until they get a 2xx, so the same event WILL arrive more
    than once — and a duplicate `subscription.charged` or `PAYMENT.CAPTURE.COMPLETED`
    processed twice extends a paid-up period, or double-fulfils an order, that the customer
    did not pay for twice. Recording the id and refusing to act on one already seen is what
    makes the handler safe to retry, which is the whole contract a webhook endpoint signs up
    to. `gateway` disambiguates the two id spaces — a collision between a Razorpay event id
    and a PayPal one is astronomically unlikely, but there's no reason to rely on that.

    The body is kept because when a subscription or payment ends up in a state nobody
    expected, the only account of what actually arrived is this table.
    """
    __tablename__ = "webhook_events"
    # Left as a single-column constraint on event_id (not (gateway, event_id)) deliberately:
    # this project has no Alembic (see grant_admin.py) and database.ensure_columns() only
    # ever ADDs columns, so an existing deployed table would keep the OLD constraint
    # regardless of what's declared here. A Razorpay event id and a PayPal one colliding is
    # astronomically unlikely — different id formats entirely — so the narrower constraint
    # already deployed is stricter than needed, not wrong, and every dedup check in code
    # filters on (gateway, event_id) anyway.
    __table_args__ = (UniqueConstraint("event_id", name="uq_webhook_event_id"),)

    id = Column(Integer, primary_key=True, index=True)
    gateway = Column(String, nullable=False, default="razorpay", index=True)
    event_id = Column(String, nullable=False, index=True)      # x-razorpay-event-id header,
                                                                # or PayPal's event `id`
    event = Column(String, nullable=True, index=True)          # e.g. subscription.charged,
                                                                # or PAYMENT.CAPTURE.COMPLETED
    payload = Column(String, nullable=True)
    handled = Column(Boolean, default=False)
    note = Column(String, nullable=True)
    received_at = Column(DateTime, default=datetime.utcnow)

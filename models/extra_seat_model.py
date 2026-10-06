from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer

from database import Base


class ExtraSeat(Base):
    """One paid team seat beyond the plan's own, bought for a month at a time.

    Active while `expires_at` is in the future; it then simply stops counting toward the
    seat limit (members already on the team are not removed — new invites stop). Renewing
    extends `expires_at` by another period (services/entitlements.grant_extra_seat).
    """
    __tablename__ = "extra_seats"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)  # team owner
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)
    last_payment_id = Column(Integer, nullable=True)

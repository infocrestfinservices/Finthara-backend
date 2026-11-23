from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from database import Base


class Feedback(Base):
    """One response to the /feedback form. Only signed-in (hence verified) users can submit,
    so `email` is the account's address — never something typed into the form."""
    __tablename__ = "feedback"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False, index=True)
    rating = Column(Integer, nullable=False)
    purpose = Column(String, nullable=False)
    quality = Column(String, nullable=False)
    recommend = Column(String, nullable=False)
    message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

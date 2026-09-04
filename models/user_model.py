from sqlalchemy import Column, Integer, String, DateTime, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String, nullable=True)
    # "free" until something is actually bought. This used to default to "starter", which is
    # the name of the ₹499 plan — so every signup was already on a paid plan and a payment
    # could not be told apart from a registration.
    plan = Column(String, default="free")
    # When a monthly plan lapses. NULL for free and for the one-time Starter, which do not
    # expire. services.entitlements.effective_plan reads this on every request rather than
    # relying on a scheduled job, so an expiry cannot be missed.
    plan_expires_at = Column(DateTime, nullable=True)
    # Staff access to the admin panel. A separate flag rather than a role string because
    # there are exactly two kinds of person here — the team and the customers — and a role
    # table nobody needs is a thing to keep in step for no benefit. Never settable through
    # any API: granted with grant_admin.py, against the database, on purpose.
    is_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_verified = Column(Boolean, default=False)
    email_verification_otp = Column(String, nullable=True)
    otp_expires_at = Column(DateTime, nullable=True)

    # ── Profile ─────────────────────────────────────────────────────────────
    phone = Column(String, nullable=True)
    # A data: URI (base64), not a file path — App Platform's filesystem is ephemeral and a
    # path saved to disk would vanish on the next deploy. Small enough (validated at upload)
    # that storing it as text in the row is fine without adding blob storage.
    avatar_url = Column(String, nullable=True)
    # "light" | "dark" | "system". Read at login so the theme follows the account across
    # devices, not just the browser that set it (next-themes still keeps its own
    # localStorage copy for the instant before that fetch returns).
    theme_preference = Column(String, default="system", nullable=False)
    notify_email = Column(Boolean, default=True, nullable=False)

    # Changing the email address takes a detour through the NEW address, the same way
    # signup does: the account keeps its current (verified) email until the new one proves
    # it can receive mail, so a typo can never lock the account out.
    pending_email = Column(String, nullable=True)
    pending_email_otp = Column(String, nullable=True)
    pending_email_otp_expires_at = Column(DateTime, nullable=True)

    # ── Two-factor auth (TOTP) ─────────────────────────────────────────────
    # Set as soon as setup starts, but totp_enabled stays False until the user proves they
    # can generate a code from it — an unconfirmed secret must never gate login.
    totp_secret = Column(String, nullable=True)
    totp_enabled = Column(Boolean, default=False, nullable=False)
    # JSON list of bcrypt-hashed one-time codes. Hashed the same way passwords are: a leaked
    # database must not hand out working backup codes any more than it hands out passwords.
    totp_backup_codes = Column(String, nullable=True)

    # Soft delete. Projects and invoices carry this user's id and, for invoices, are a legal
    # record — hard-deleting the row would either cascade into deleting those or leave them
    # pointing at nothing. Deactivating keeps the account unreachable (login refuses it)
    # without destroying anything it is attached to.
    is_deleted = Column(Boolean, default=False, nullable=False)
    deleted_at = Column(DateTime, nullable=True)

    projects = relationship("Project", back_populates="user")

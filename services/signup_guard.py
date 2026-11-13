"""Keeping bots from using the sign-up, OTP and password-reset forms to send email.

What it defends against: scripts that fill sign-up forms across the web with someone else's
address so that person's inbox is buried in "verify your email" mail ("subscription
bombing"). Every one of those mails goes out from our domain to a stranger who marks it as
spam, which is how a sending domain ends up in the spam folder for real customers too.

Three pieces:

* **throttle()** — a sliding-window counter, in memory. The API runs as a single uvicorn
  worker (deploy/reportcraft-api.service), so one process sees every request; a restart
  resets the counts, which is acceptable for a brake rather than a lock.
* **canonical_email()** — Gmail ignores dots and anything after "+", so
  ``y.a.re.v.er@gmail.com`` and ``yarever+x@gmail.com`` are one inbox. Bots rotate those forms
  to get past "already registered"; limits and duplicate checks use the canonical form.
* **purge_unverified()** — unverified accounts older than a few days are deleted. A real
  person verifies within minutes; what is left after days is bot residue.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from fastapi import HTTPException, Request
from sqlalchemy import func

logger = logging.getLogger("signup_guard")

# ── who is asking ──────────────────────────────────────────────────────────────

_LOCAL = {"127.0.0.1", "::1", "localhost"}


def client_ip(request: Request) -> str:
    """The caller's IP.

    Behind nginx every request arrives from 127.0.0.1, so throttling on request.client alone
    would put the whole internet in one bucket. nginx sets X-Real-IP itself (overwriting
    anything the client sent), so it is trusted — but only when the request really came from
    the local proxy; a direct caller could put anything in that header.
    """
    direct = request.client.host if request.client else "unknown"
    if direct in _LOCAL:
        real = (request.headers.get("x-real-ip") or "").strip()
        if real:
            return real
    return direct


# ── throttle ───────────────────────────────────────────────────────────────────

_HITS: dict[str, list[float]] = {}


def hit(key: str, limit: int, window_sec: int) -> bool:
    """Record one attempt for `key`; True when that attempt goes over the limit."""
    now = time.time()
    hits = [t for t in _HITS.get(key, []) if now - t < window_sec]
    hits.append(now)
    _HITS[key] = hits
    if len(_HITS) > 20000:                       # bound the memory; a reset is harmless
        _HITS.clear()
    return len(hits) > limit


def throttle(key: str, limit: int, window_sec: int, message: str) -> None:
    """Raise 429 when `key` is over its limit."""
    if hit(key, limit, window_sec):
        logger.warning("signup_guard: throttled %s", key)
        raise HTTPException(status_code=429, detail=message)


TOO_MANY = "Too many attempts. Please wait a while and try again."

# Limits. Generous for a person (who signs up once and maybe asks for one more code), tight
# for a script.
IP_SIGNUPS = (5, 600)            # 5 sign-ups per IP per 10 minutes
IP_EMAILS = (10, 3600)           # 10 OTP / reset requests per IP per hour
EMAIL_SENDS = (3, 3600)          # 3 mails to one inbox per hour, whatever the IP


# ── Gmail dot / plus trick ─────────────────────────────────────────────────────

_GMAIL = {"gmail.com", "googlemail.com"}


def canonical_email(email: str) -> str:
    """The inbox an address actually delivers to. Only Gmail is folded — other providers do
    treat dots as significant, and folding them would merge different people."""
    email = (email or "").strip().lower()
    local, _, domain = email.partition("@")
    if domain in _GMAIL:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def is_gmail(email: str) -> bool:
    return email.strip().lower().rpartition("@")[2] in _GMAIL


def find_same_inbox(db, email: str, exclude_exact: bool = True):
    """Another account whose address is the same Gmail inbox as `email` (dots / +tag
    differences only), or None."""
    from models.user_model import User
    if not is_gmail(email):
        return None
    canon = canonical_email(email)
    local = canon.split("@")[0]
    # Narrow in SQL (the dot-less local part must appear), confirm in Python (the +tag and
    # the googlemail alias cannot be folded portably in SQL).
    q = db.query(User).filter(
        or_gmail(User),
        func.replace(func.lower(User.email), ".", "").like(f"{local}%"))
    for u in q.limit(50).all():
        if exclude_exact and u.email.lower() == email.strip().lower():
            continue
        if canonical_email(u.email) == canon:
            return u
    return None


def or_gmail(User):
    from sqlalchemy import or_
    e = func.lower(User.email)
    return or_(e.like("%@gmail.com"), e.like("%@googlemail.com"))


# ── clean-up ───────────────────────────────────────────────────────────────────

UNVERIFIED_MAX_AGE = timedelta(days=3)


def purge_unverified(db, older_than: timedelta = UNVERIFIED_MAX_AGE) -> int:
    """Delete accounts that never verified their email and are older than `older_than`.

    Only accounts with nothing attached are removed: each delete runs in its own savepoint,
    and one that any other table still points at is skipped rather than breaking the rest.
    An unverified account cannot log in, so in practice none has anything attached.
    """
    from models.user_model import User
    cutoff = datetime.utcnow() - older_than
    rows = (db.query(User)
              .filter(User.is_verified.is_(False) | User.is_verified.is_(None),
                      User.is_admin.is_(False),
                      User.created_at < cutoff,
                      # The ORM would set a project's user_id to NULL rather than fail, so
                      # anyone with a project is excluded here, not left to the savepoint.
                      ~User.projects.any())
              .all())
    removed = 0
    for u in rows:
        try:
            with db.begin_nested():
                db.delete(u)
                db.flush()
            removed += 1
        except Exception:
            logger.info("signup_guard: kept unverified user %s (still referenced)", u.id)
    db.commit()
    if removed:
        logger.info("signup_guard: removed %d unverified account(s) older than %s",
                    removed, older_than)
    return removed

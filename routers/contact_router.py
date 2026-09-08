"""Public contact form.

GET  /contact  — the details a "Contact us" page shows (support email, company, address).
POST /contact  — a message from that page, emailed to COMPANY_EMAIL via Resend.

Unauthenticated by nature (a prospect has no account). Guarded by a honeypot field and a
best-effort per-IP throttle — there is no rate-limiting middleware in this project, and a
public POST that sends email is a spam vector without one.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, field_validator

from config import settings
from services.email_service import send_contact_message, email_configured

logger = logging.getLogger("contact")

router = APIRouter(prefix="/contact", tags=["Contact"])

# ip -> [timestamps] within the window. In-memory, per-process, best effort — a restart or a
# second worker resets it, which is fine for what this defends against.
_HITS: dict[str, list[float]] = {}
_WINDOW_SEC = 3600
_MAX_PER_WINDOW = 5


def _throttled(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _HITS.get(ip, []) if now - t < _WINDOW_SEC]
    hits.append(now)
    _HITS[ip] = hits
    if len(_HITS) > 5000:                       # keep the dict from growing without bound
        _HITS.clear()
    return len(hits) > _MAX_PER_WINDOW


class ContactRequest(BaseModel):
    name: str
    email: EmailStr
    message: str
    topic: str | None = None
    # Honeypot: a real user never fills this (it's hidden in the form). A bot fills every field.
    company: str | None = None

    @field_validator("name", "message")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("This field is required.")
        return v

    @field_validator("message")
    @classmethod
    def _length(cls, v: str) -> str:
        if len(v) > 5000:
            raise ValueError("Message is too long.")
        return v


@router.get("")
@router.get("/")
def contact_info():
    return {
        "company": settings.COMPANY_NAME,
        "email": settings.COMPANY_EMAIL or settings.FROM_EMAIL,
        "address": settings.COMPANY_ADDRESS or None,
        "form_enabled": email_configured(),
    }


@router.post("")
@router.post("/")
def submit_contact(req: ContactRequest, request: Request):
    if req.company:                              # honeypot tripped — pretend it worked
        return {"ok": True}

    ip = (request.client.host if request.client else "unknown")
    if _throttled(ip):
        raise HTTPException(status_code=429,
                            detail="Too many messages from here. Try again later or email us directly.")

    sent = False
    try:
        sent = send_contact_message(name=req.name.strip(), from_email=str(req.email),
                                    message=req.message.strip(),
                                    topic=(req.topic or None))
    except Exception:
        logger.exception("contact: could not send message from %s", req.email)
        raise HTTPException(status_code=502,
                            detail="Couldn't send your message. Please email us directly.")

    if not sent:
        # Email isn't configured on this server — tell the page so it shows the mailto: link.
        raise HTTPException(status_code=503,
                            detail=f"Please email us at {settings.COMPANY_EMAIL or settings.FROM_EMAIL}.")

    logger.info("contact: message from %s (topic=%s)", req.email, req.topic)
    return {"ok": True}

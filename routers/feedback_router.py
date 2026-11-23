"""Public feedback form (footer "Feedback" link and /feedback page).

POST /feedback — a 5-question feedback response, emailed to FEEDBACK_EMAIL (super admin)
via Resend. Nothing is stored in the database.

Unauthenticated (anyone on the site can give feedback), so it uses the same honeypot +
best-effort per-IP throttle as the contact form.
"""
from __future__ import annotations

import logging
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field, field_validator

from services.email_service import send_feedback_message

logger = logging.getLogger("feedback")

router = APIRouter(prefix="/feedback", tags=["Feedback"])

_HITS: dict[str, list[float]] = {}
_WINDOW_SEC = 3600
_MAX_PER_WINDOW = 5

# Must match the option lists in frontend/src/components/FeedbackForm.jsx.
Purpose = Literal["CMA / Bank Loan", "Feasibility Study", "Short Project Report", "Just exploring"]
Quality = Literal["Excellent", "Good", "Average", "Poor"]
Recommend = Literal["Yes", "Maybe", "No"]


def _throttled(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _HITS.get(ip, []) if now - t < _WINDOW_SEC]
    hits.append(now)
    _HITS[ip] = hits
    if len(_HITS) > 5000:
        _HITS.clear()
    return len(hits) > _MAX_PER_WINDOW


class FeedbackRequest(BaseModel):
    rating: int = Field(ge=1, le=5)
    purpose: Purpose
    quality: Quality
    recommend: Recommend
    message: str = ""
    name: str | None = None
    email: EmailStr | None = None
    # Honeypot: hidden in the form, only bots fill it.
    company: str | None = None

    @field_validator("email", mode="before")
    @classmethod
    def _blank_email(cls, v):
        return v or None            # "" from an untouched optional field → None

    @field_validator("message")
    @classmethod
    def _length(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) > 5000:
            raise ValueError("Message is too long.")
        return v


@router.post("")
@router.post("/")
def submit_feedback(req: FeedbackRequest, request: Request):
    if req.company:                              # honeypot tripped — pretend it worked
        return {"ok": True}

    from services.signup_guard import client_ip
    if _throttled(client_ip(request)):
        raise HTTPException(status_code=429,
                            detail="Too many submissions from here. Please try again later.")

    try:
        sent = send_feedback_message(
            rating=req.rating, purpose=req.purpose, quality=req.quality,
            recommend=req.recommend, message=req.message,
            name=(req.name or "").strip() or None,
            from_email=str(req.email) if req.email else None,
        )
    except Exception:
        logger.exception("feedback: could not send response")
        # 409 not 5xx: App Platform's edge replaces 5xx bodies with its own page.
        raise HTTPException(status_code=409, detail="Couldn't send your feedback. Please try again.")

    if not sent:
        raise HTTPException(status_code=409, detail="Feedback isn't available right now.")

    logger.info("feedback: %s/5 (%s)", req.rating, req.purpose)
    return {"ok": True}

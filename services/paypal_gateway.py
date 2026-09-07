"""PayPal, over their REST Orders v2 API directly — no PayPal SDK on this side, the same way
services/subscriptions.py talks to Razorpay through its own client rather than a framework.

The shape mirrors the Razorpay integration on purpose: **the price is decided by the server**,
an order is created for that server-side amount, and the money is only real once PayPal's
capture response says `COMPLETED` — never on the strength of the browser saying "the buyer
approved it". Approval is the buyer's consent to pay; capture is PayPal actually taking the
money, and the two can be minutes apart or never happen at all (an approved order the buyer
never completes, a card that gets declined at capture time).

Everything here is synchronous (httpx.Client, not AsyncClient) to match payment_router.py's
plain `def` endpoints — the Razorpay SDK calls in that file are sync for the same reason.
"""
from __future__ import annotations

import logging
import threading
import time

import httpx

from config import settings

logger = logging.getLogger("paypal")

_TIMEOUT = 20.0

# ── OAuth ──────────────────────────────────────────────────────────────────────────────
# One token, shared by every request this process makes. PayPal's client-credentials tokens
# run about 9 hours; re-authenticating on every API call would work but is a slow, unnecessary
# round-trip on the hot path of a checkout. A lock guards the refresh because two requests
# arriving with the cache empty must not both spend a credential exchange — the second should
# just wait a few milliseconds and read what the first fetched.
_token_lock = threading.Lock()
_token_cache: dict = {"value": None, "expires_at": 0.0}

# Refreshed this long before PayPal says it actually expires, so a token is never handed to a
# request that then has it expire mid-flight.
_EXPIRY_MARGIN = 300.0


def _fetch_token() -> tuple[str, float]:
    resp = httpx.post(
        f"{settings.paypal_api_base}/v1/oauth2/token",
        data={"grant_type": "client_credentials"},
        auth=(settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    return body["access_token"], time.time() + float(body.get("expires_in", 32000))


def _access_token(force: bool = False) -> str:
    if not force:
        cached = _token_cache["value"]
        if cached and time.time() < _token_cache["expires_at"] - _EXPIRY_MARGIN:
            return cached
    with _token_lock:
        # Re-check inside the lock: another thread may have refreshed it while this one
        # was waiting to acquire.
        cached = _token_cache["value"]
        if not force and cached and time.time() < _token_cache["expires_at"] - _EXPIRY_MARGIN:
            return cached
        token, expires_at = _fetch_token()
        _token_cache["value"], _token_cache["expires_at"] = token, expires_at
        return token


class PayPalError(Exception):
    """A PayPal API call failed. `detail` is their parsed JSON error body when there was one,
    so a caller can look for a specific `issue` (e.g. ORDER_ALREADY_CAPTURED) rather than
    string-matching the message."""
    def __init__(self, message: str, status_code: int | None = None, detail: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail or {}


def _request(method: str, path: str, *, json: dict | None = None,
             idempotency_key: str | None = None, _retried: bool = False) -> dict:
    headers = {"Authorization": f"Bearer {_access_token()}"}
    if idempotency_key:
        headers["PayPal-Request-Id"] = idempotency_key
    resp = httpx.request(method, f"{settings.paypal_api_base}{path}",
                         json=json, headers=headers, timeout=_TIMEOUT)
    if resp.status_code == 401 and not _retried:
        # The cached token was rejected — refresh once and retry, rather than surfacing an
        # auth error for what is really just an expired cache entry.
        logger.info("paypal: got 401, refreshing the access token and retrying once")
        _access_token(force=True)
        return _request(method, path, json=json, idempotency_key=idempotency_key, _retried=True)
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {}
    if resp.status_code >= 400:
        raise PayPalError(
            f"PayPal {method} {path} returned {resp.status_code}",
            status_code=resp.status_code, detail=body)
    return body


# ── Orders ─────────────────────────────────────────────────────────────────────────────

def create_order(*, amount: str, currency: str, reference_id: str, custom_id: str,
                 idempotency_key: str) -> dict:
    """POST /v2/checkout/orders. `amount` is a STRING in major units ("49.00"), the way
    PayPal's API wants it — not paise/cents, and not a float (float("49.00") round-trips fine
    here, but the caller formats it so this function never has to guess a precision)."""
    return _request("POST", "/v2/checkout/orders", idempotency_key=idempotency_key, json={
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": reference_id,
            "custom_id": custom_id,
            "amount": {"currency_code": currency, "value": amount},
        }],
        "application_context": {
            "brand_name": settings.PAYPAL_BRAND_NAME,
            "shipping_preference": "NO_SHIPPING",
            "user_action": "PAY_NOW",
        },
    })


def get_order(order_id: str) -> dict:
    return _request("GET", f"/v2/checkout/orders/{order_id}")


def capture_order(order_id: str) -> dict:
    """POST .../capture, with a DETERMINISTIC idempotency key: two callers racing to capture
    the same order (the browser callback and the webhook, most likely) both send
    `capture-{order_id}` and PayPal returns the SAME result to both rather than attempting a
    second charge. If PayPal says the order was already captured, the existing capture is
    fetched and returned in the same shape instead of raising — that outcome (captured) is not
    an error, it is what a retry was hoping to confirm.
    """
    try:
        return _request("POST", f"/v2/checkout/orders/{order_id}/capture",
                        idempotency_key=f"capture-{order_id}", json={})
    except PayPalError as e:
        issue = ((e.detail or {}).get("details") or [{}])[0].get("issue")
        if e.status_code == 422 and issue == "ORDER_ALREADY_CAPTURED":
            logger.info("paypal: order %s was already captured, reading the existing result",
                       order_id)
            return get_order(order_id)
        raise


def capture_status(order: dict) -> str | None:
    """The COMPLETED/PENDING/DECLINED status of an order's (first) capture, or None if it
    has not been captured at all yet — pulled out of the nested capture object because every
    caller needs this and the payload shape is not obvious at a glance."""
    try:
        return order["purchase_units"][0]["payments"]["captures"][0]["status"]
    except (KeyError, IndexError, TypeError):
        return None


def captured_amount(order: dict) -> tuple[str, str] | None:
    """(value, currency_code) actually captured, or None. Compared against what we expected
    to be charged before a payment is ever marked paid — see routers/paypal_router.py."""
    try:
        cap = order["purchase_units"][0]["payments"]["captures"][0]
        amt = cap["amount"]
        return amt["value"], amt["currency_code"]
    except (KeyError, IndexError, TypeError):
        return None


def capture_id(order: dict) -> str | None:
    try:
        return order["purchase_units"][0]["payments"]["captures"][0]["id"]
    except (KeyError, IndexError, TypeError):
        return None


# ── Webhooks ───────────────────────────────────────────────────────────────────────────

def verify_webhook_signature(*, headers, raw_body: bytes, webhook_id: str) -> bool:
    """POST /v1/notifications/verify-webhook-signature — this call, not a locally-computed
    HMAC, IS the verification: PayPal checks the transmission signature against the event and
    tells us SUCCESS or FAILURE.

    `raw_body` must be the exact bytes PayPal sent, parsed here and ONLY here for this one
    call — never re-serialized from a framework's already-parsed request object first, which
    can reorder keys or change whitespace and make a genuine delivery fail verification.
    `headers` is whatever case-insensitive mapping the framework hands back (FastAPI's
    Request.headers works as-is).
    """
    if not webhook_id:
        logger.warning("paypal: webhook rejected — PAYPAL_WEBHOOK_ID is not configured")
        return False
    try:
        import json as _json
        event = _json.loads(raw_body)
    except ValueError:
        return False

    required = ("paypal-transmission-id", "paypal-transmission-time", "paypal-cert-url",
               "paypal-auth-algo", "paypal-transmission-sig")
    if any(not headers.get(h) for h in required):
        logger.warning("paypal: webhook rejected — missing a required PayPal-* header")
        return False

    try:
        result = _request("POST", "/v1/notifications/verify-webhook-signature", json={
            "transmission_id": headers.get("paypal-transmission-id"),
            "transmission_time": headers.get("paypal-transmission-time"),
            "cert_url": headers.get("paypal-cert-url"),
            "auth_algo": headers.get("paypal-auth-algo"),
            "transmission_sig": headers.get("paypal-transmission-sig"),
            "webhook_id": webhook_id,
            "webhook_event": event,
        })
    except PayPalError:
        logger.exception("paypal: webhook signature verification call failed")
        return False
    return result.get("verification_status") == "SUCCESS"

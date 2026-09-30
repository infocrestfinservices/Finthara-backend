"""Cashfree Payment Gateway — the thin layer over the official SDK (cashfree_pg).

Everything that talks to Cashfree lives here, so payment_router.py deals only in our own
terms: create an order for an amount, ask what happened to it, check a webhook's signature.

Two things worth knowing about Cashfree:

**Amounts are in RUPEES**, as a decimal (1999.00) — not paise, unlike Razorpay. The order is
created with the rupee amount straight from services/entitlements.py (after any coupon), and
the same number is what a PAID order must come back with.

**The webhook is signed with the API secret key**: signature = base64(HMAC-SHA256(
timestamp + raw_body, CASHFREE_SECRET_KEY)), sent as x-webhook-signature with the timestamp
in x-webhook-timestamp. The raw bytes matter — re-serialising the parsed JSON changes spacing
and key order and the signature would never match.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging

from config import settings

logger = logging.getLogger("cashfree")

_TIMEOUT = 20  # seconds, per call


class CashfreeError(Exception):
    """A Cashfree API call failed. The message is safe to show the customer."""


def enabled() -> bool:
    return settings.payments_enabled


def mode() -> str:
    """What the browser's cashfree.js must be initialised with."""
    return "production" if settings.cashfree_production else "sandbox"


def _verify_tls():
    """The SDK (cashfree_pg 6.0.1, rest.py) hard-codes `cert_reqs = ssl.CERT_NONE`: it never
    checks Cashfree's certificate, so every call carrying our secret key could be read by
    anyone able to intercept the connection. Every call goes through one shared ApiClient,
    so its connection pool is swapped once for one that verifies against certifi's CAs."""
    import certifi
    import urllib3
    from cashfree_pg.api_client import ApiClient
    rest = ApiClient.get_default().rest_client
    if getattr(rest, "_tls_verified", False):
        return
    rest.pool_manager = urllib3.PoolManager(num_pools=4, maxsize=4, cert_reqs="CERT_REQUIRED",
                                            ca_certs=certifi.where())
    rest._tls_verified = True


def _client():
    from cashfree_pg.api_client import Cashfree
    _verify_tls()
    return Cashfree(
        XEnvironment=Cashfree.PRODUCTION if settings.cashfree_production else Cashfree.SANDBOX,
        XClientId=settings.CASHFREE_APP_ID.strip(),
        XClientSecret=settings.CASHFREE_SECRET_KEY.strip(),
    )


def _explain(exc) -> str:
    """Cashfree's own error message out of an SDK exception, when it has one."""
    body = getattr(exc, "body", None)
    if body:
        try:
            import json
            data = json.loads(body)
            return data.get("message") or str(exc)
        except Exception:
            return str(body)[:300]
    return str(exc)[:300]


def create_order(*, order_id: str, amount: float, customer_id: str, customer_email: str,
                 customer_phone: str, customer_name: str = "", return_url: str | None = None,
                 note: str = "") -> dict:
    """Create an order and return {"order_id", "cf_order_id", "payment_session_id"}.

    `order_id` is ours (unique per attempt) and is reused as the idempotency key, so a retry
    after a timeout cannot create a second order for the same attempt.
    """
    from cashfree_pg.models.create_order_request import CreateOrderRequest
    from cashfree_pg.models.customer_details import CustomerDetails
    from cashfree_pg.models.order_meta import OrderMeta

    def _fits(value, lo=3, hi=100):
        """Cashfree rejects a name or email outside 3–100 characters; leave it out instead."""
        value = (value or "").strip()
        return value if lo <= len(value) <= hi else None

    try:
        request = CreateOrderRequest(
            order_id=order_id,
            order_amount=round(float(amount), 2),
            order_currency="INR",
            customer_details=CustomerDetails(
                customer_id=customer_id,
                customer_email=_fits(customer_email),
                customer_phone=customer_phone,
                customer_name=_fits(customer_name),
            ),
            order_meta=OrderMeta(return_url=return_url) if return_url else None,
            order_note=_fits(note, 3, 200),
        )
        resp = _client().PGCreateOrder(request, None, order_id, _request_timeout=_TIMEOUT)
    except Exception as e:
        logger.exception("cashfree: create order %s failed", order_id)
        raise CashfreeError(_explain(e)) from e
    data = resp.data
    if not data or not data.payment_session_id:
        raise CashfreeError("Cashfree did not return a payment session.")
    return {"order_id": data.order_id, "cf_order_id": str(data.cf_order_id or ""),
            "payment_session_id": data.payment_session_id}


def fetch_order(order_id: str) -> dict:
    """{"status": ACTIVE|PAID|EXPIRED|TERMINATED|..., "amount": float, "currency": str}."""
    try:
        resp = _client().PGFetchOrder(order_id, None, None, _request_timeout=_TIMEOUT)
    except Exception as e:
        logger.exception("cashfree: fetch order %s failed", order_id)
        raise CashfreeError(_explain(e)) from e
    o = resp.data
    return {"status": str(o.order_status or "").upper(),
            "amount": float(o.order_amount or 0),
            "currency": str(o.order_currency or "").upper()}


def successful_payment(order_id: str) -> dict | None:
    """The SUCCESS payment on an order, as {"cf_payment_id", "amount", "currency"}, or None."""
    try:
        resp = _client().PGOrderFetchPayments(order_id, None, None, _request_timeout=_TIMEOUT)
    except Exception as e:
        logger.exception("cashfree: fetch payments for %s failed", order_id)
        raise CashfreeError(_explain(e)) from e
    for p in resp.data or []:
        if str(p.payment_status or "").upper() == "SUCCESS":
            return {"cf_payment_id": str(p.cf_payment_id or ""),
                    "amount": float(p.payment_amount or 0),
                    "currency": str(p.payment_currency or "").upper()}
    return None


def verify_webhook(raw_body: bytes, signature: str, timestamp: str) -> bool:
    """True only if the delivery was signed by Cashfree with our secret key.

    Fails closed: no secret configured, or either header missing, means rejected. Compared in
    constant time (the SDK's own helper uses ==).
    """
    secret = settings.CASHFREE_SECRET_KEY.strip()
    if not secret or not signature or not timestamp:
        return False
    message = timestamp.encode() + raw_body
    expected = base64.b64encode(hmac.new(secret.encode(), message, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, signature.strip())

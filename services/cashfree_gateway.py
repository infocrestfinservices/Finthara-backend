"""Cashfree Payment Gateway — its REST API, called directly with `requests`.

Everything that talks to Cashfree lives here, so payment_router.py deals only in our own
terms: create an order for an amount, ask what happened to it, check a webhook's signature.

Why not the official SDK (cashfree_pg): every version pins urllib3 < 2.1.0, which would drag
the whole backend onto an old urllib3 with fixed-since security issues, and its HTTP client
hard-codes `ssl.CERT_NONE` — it never checks Cashfree's certificate. Three plain HTTPS calls
with normal TLS verification are simpler and safer.

API reference (x-api-version 2026-01-01), https://www.cashfree.com/docs/api-reference/payments/latest:
  POST /pg/orders                          create an order -> payment_session_id
  GET  /pg/orders/{order_id}               order_status: ACTIVE | PAID | EXPIRED | TERMINATED | ...
  GET  /pg/orders/{order_id}/payments      JSON array; payment_status SUCCESS | FAILED | ...
Auth headers on every call: x-client-id, x-client-secret, x-api-version. Errors come back as
{"message", "code", "type"} with a 4xx/5xx status.

**Amounts are in RUPEES**, as a decimal (1999.00) — not paise. The order is created with the
rupee amount straight from services/entitlements.py (after any coupon), and the same number is
what a PAID order must come back with.

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
from urllib.parse import quote

import requests

from config import settings

logger = logging.getLogger("cashfree")

API_VERSION = "2026-01-01"
_HOSTS = {"sandbox": "https://sandbox.cashfree.com/pg",
          "production": "https://api.cashfree.com/pg"}
_TIMEOUT = (10, 20)  # seconds: connect, read


class CashfreeError(Exception):
    """A Cashfree API call failed. The message is safe to show the customer."""


def enabled() -> bool:
    return settings.payments_enabled


def mode() -> str:
    """Which Cashfree environment this server uses — also what cashfree.js is opened with."""
    return "production" if settings.cashfree_production else "sandbox"


def _call(method: str, path: str, *, body: dict | None = None,
          idempotency_key: str | None = None):
    """One authenticated call. Returns the parsed JSON; raises CashfreeError otherwise.
    TLS is verified (requests' default) — never turned off."""
    headers = {
        "x-client-id": settings.CASHFREE_APP_ID.strip(),
        "x-client-secret": settings.CASHFREE_SECRET_KEY.strip(),
        "x-api-version": API_VERSION,
        "Accept": "application/json",
    }
    if idempotency_key:
        headers["x-idempotency-key"] = idempotency_key
    url = _HOSTS[mode()] + path
    try:
        resp = requests.request(method, url, headers=headers, json=body, timeout=_TIMEOUT)
    except requests.RequestException as e:
        logger.warning("cashfree: %s %s failed to connect: %s", method, path, e)
        raise CashfreeError("Could not reach Cashfree. Please try again.") from e
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code >= 400:
        message = (data or {}).get("message") if isinstance(data, dict) else None
        logger.warning("cashfree: %s %s -> %s %s", method, path, resp.status_code,
                       (data or {}).get("code") if isinstance(data, dict) else "")
        raise CashfreeError(message or f"Cashfree returned HTTP {resp.status_code}.")
    if data is None:
        raise CashfreeError("Cashfree returned an unreadable response.")
    return data


def _fits(value, lo=3, hi=100):
    """Cashfree rejects a name, email or note outside its length limits; leave it out."""
    value = (value or "").strip()
    return value if lo <= len(value) <= hi else None


def create_order(*, order_id: str, amount: float, customer_id: str, customer_email: str,
                 customer_phone: str, customer_name: str = "", return_url: str | None = None,
                 note: str = "") -> dict:
    """Create an order and return {"order_id", "cf_order_id", "payment_session_id"}.

    `order_id` is ours (unique per attempt) and doubles as the idempotency key, so a retry
    after a timeout cannot create a second order for the same attempt.
    """
    customer = {"customer_id": customer_id, "customer_phone": customer_phone}
    if _fits(customer_email):
        customer["customer_email"] = _fits(customer_email)
    if _fits(customer_name):
        customer["customer_name"] = _fits(customer_name)
    body = {
        "order_id": order_id,
        "order_amount": round(float(amount), 2),
        "order_currency": "INR",
        "customer_details": customer,
    }
    if return_url:
        body["order_meta"] = {"return_url": return_url}
    if _fits(note, 3, 200):
        body["order_note"] = _fits(note, 3, 200)

    data = _call("POST", "/orders", body=body, idempotency_key=order_id)
    if not data.get("payment_session_id"):
        raise CashfreeError("Cashfree did not return a payment session.")
    return {"order_id": data.get("order_id"), "cf_order_id": str(data.get("cf_order_id") or ""),
            "payment_session_id": data["payment_session_id"]}


def fetch_order(order_id: str) -> dict:
    """{"status": ACTIVE|PAID|EXPIRED|TERMINATED|..., "amount": float, "currency": str}."""
    o = _call("GET", f"/orders/{quote(order_id, safe='')}")
    return {"status": str(o.get("order_status") or "").upper(),
            "amount": float(o.get("order_amount") or 0),
            "currency": str(o.get("order_currency") or "").upper()}


def successful_payment(order_id: str) -> dict | None:
    """The SUCCESS payment on an order, as {"cf_payment_id", "amount", "currency"}, or None."""
    payments = _call("GET", f"/orders/{quote(order_id, safe='')}/payments")
    for p in payments if isinstance(payments, list) else []:
        if str(p.get("payment_status") or "").upper() == "SUCCESS":
            return {"cf_payment_id": str(p.get("cf_payment_id") or ""),
                    "amount": float(p.get("payment_amount") or 0),
                    "currency": str(p.get("payment_currency") or "").upper()}
    return None


def verify_webhook(raw_body: bytes, signature: str, timestamp: str) -> bool:
    """True only if the delivery was signed by Cashfree with our secret key.

    Fails closed: no secret configured, or either header missing, means rejected. Compared in
    constant time.
    """
    secret = settings.CASHFREE_SECRET_KEY.strip()
    if not secret or not signature or not timestamp:
        return False
    message = timestamp.encode() + raw_body
    expected = base64.b64encode(hmac.new(secret.encode(), message, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, signature.strip())

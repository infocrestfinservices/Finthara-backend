import httpx

from config import settings

RESEND_ENDPOINT = "https://api.resend.com/emails"

# Sync, unlike everything above it: this is called from payment_router.py and
# paypal_router.py, which are plain `def` endpoints (the Razorpay/PayPal SDK calls in them
# are sync too), not `async def` like auth_router.py. A plain httpx.post avoids needing an
# event loop inside a sync request handler.
_RESEND_TIMEOUT = 15.0


def email_configured() -> bool:
    """True when an email provider (Resend) is set up.

    When this is False the auth flow skips real sending and surfaces the OTP
    directly (dev_otp) so testing isn't blocked.
    """
    return bool(settings.RESEND_API_KEY)


def _otp_email_html(full_name: str, otp: str) -> str:
    name = full_name or "there"
    return f"""\
<div style="font-family: Arial, Helvetica, sans-serif; max-width: 480px; margin: 0 auto; color: #0F172A;">
  <h2 style="color: #1B3A6B; margin-bottom: 8px;">Verify your email</h2>
  <p style="margin: 0 0 16px;">Hi {name},</p>
  <p style="margin: 0 0 16px;">
    Use the verification code below to finish setting up your
    {settings.FROM_NAME} account. This code expires in 10 minutes.
  </p>
  <div style="font-size: 32px; font-weight: bold; letter-spacing: 8px;
              color: #2563EB; background: #EFF6FF; padding: 16px 0;
              text-align: center; border-radius: 8px; margin: 0 0 16px;">
    {otp}
  </div>
  <p style="margin: 0; font-size: 13px; color: #64748B;">
    If you didn't request this, you can safely ignore this email.
  </p>
</div>"""


async def send_verification_email(email: str, full_name: str, otp: str) -> None:
    """Send a verification OTP to the user via Resend.

    No-op when email isn't configured, so registration can still proceed with
    a dev_otp. Raises on a genuine send failure; the caller rolls back and
    surfaces the error.
    """
    if not email_configured():
        return

    sender = f"{settings.FROM_NAME} <{settings.FROM_EMAIL}>"
    payload = {
        "from": sender,
        "to": [email],
        "subject": f"Your {settings.FROM_NAME} verification code",
        "html": _otp_email_html(full_name, otp),
    }
    headers = {
        "Authorization": f"Bearer {settings.RESEND_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(RESEND_ENDPOINT, json=payload, headers=headers)

    if response.status_code >= 400:
        raise RuntimeError(
            f"Resend API returned {response.status_code}: {response.text}"
        )


def _reset_email_html(full_name: str, link: str, minutes: int) -> str:
    name = full_name or "there"
    return f"""\
<div style="font-family: Arial, Helvetica, sans-serif; max-width: 480px; margin: 0 auto; color: #0F172A;">
  <h2 style="color: #1B3A6B; margin-bottom: 8px;">Reset your password</h2>
  <p style="margin: 0 0 16px;">Hi {name},</p>
  <p style="margin: 0 0 16px;">
    Someone asked to reset the password on your {settings.FROM_NAME} account. Use the
    button below to choose a new one. The link works for {minutes} minutes.
  </p>
  <p style="margin: 0 0 20px;">
    <a href="{link}"
       style="display: inline-block; background: #1B3A6B; color: #ffffff;
              padding: 12px 22px; border-radius: 8px; text-decoration: none;
              font-weight: bold;">Choose a new password</a>
  </p>
  <p style="margin: 0 0 16px; font-size: 13px; color: #64748B;">
    If the button doesn't work, paste this into your browser:<br>
    <span style="word-break: break-all; color: #2563EB;">{link}</span>
  </p>
  <p style="margin: 0; font-size: 13px; color: #64748B;">
    If you didn't ask for this, you can ignore this email — your password has not changed,
    and nobody can use this link without opening it from your inbox.
  </p>
</div>"""


async def send_password_reset_email(email: str, full_name: str, link: str,
                                    minutes: int) -> bool:
    """Send a password-reset link. Returns True if it was actually sent.

    Returns False rather than raising when email is not configured, because the caller must
    behave the same either way: the endpoint's answer can never depend on whether the send
    succeeded, or it would tell a stranger which addresses have accounts.
    """
    if not email_configured():
        return False

    payload = {
        "from": f"{settings.FROM_NAME} <{settings.FROM_EMAIL}>",
        "to": [email],
        "subject": f"Reset your {settings.FROM_NAME} password",
        "html": _reset_email_html(full_name, link, minutes),
    }
    headers = {
        "Authorization": f"Bearer {settings.RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(RESEND_ENDPOINT, json=payload, headers=headers)
        r.raise_for_status()
    return True


def _purchase_email_text(full_name: str, invoice_number: str, plan_label: str,
                         amount: float, currency: str, discount: float) -> str:
    name = full_name or "there"
    lines = [
        f"Hi {name},",
        "",
        f"Thanks for your payment — your {plan_label} plan is now active.",
        "",
        f"Invoice: {invoice_number}",
        f"Amount paid: {currency} {amount:,.2f}",
    ]
    if discount:
        lines.append(f"Discount applied: {currency} {discount:,.2f}")
    lines += [
        "",
        f"This invoice, and every one before it, is available any time under Billing "
        f"History: {settings.FRONTEND_URL.rstrip('/')}/account",
        "",
        f"Questions about this charge? Reply to this email or write to "
        f"{settings.COMPANY_EMAIL or settings.FROM_EMAIL}.",
        "",
        f"— {settings.FROM_NAME}",
    ]
    return "\n".join(lines)


def send_plan_purchase_email(email: str, full_name: str, *, invoice_number: str,
                             plan_label: str, amount: float, currency: str,
                             discount: float = 0.0) -> None:
    """A plain-text receipt: which invoice number, what was paid, where to find every past
    one. No PDF attached — the same information plus the downloadable PDF is already sitting
    under Billing History, so attaching it here would just be a second copy to keep in sync.

    Sync (see the note above the imports) and, like the OTP email, a no-op when email is not
    configured — a missing SMTP/Resend setup on a dev box must not turn a successful payment
    into an error, which is also why the CALLER wraps this in try/except and only logs a
    failure rather than letting it interrupt anything.
    """
    if not email_configured():
        return

    payload = {
        "from": f"{settings.FROM_NAME} <{settings.FROM_EMAIL}>",
        "to": [email],
        "subject": f"Your {settings.FROM_NAME} invoice {invoice_number}",
        "text": _purchase_email_text(full_name, invoice_number, plan_label, amount,
                                     currency, discount),
    }
    headers = {
        "Authorization": f"Bearer {settings.RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    response = httpx.post(RESEND_ENDPOINT, json=payload, headers=headers,
                          timeout=_RESEND_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f"Resend API returned {response.status_code}: {response.text}")

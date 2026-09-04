"""Two-factor auth (TOTP), and the backup codes that stand in for a lost phone.

pyotp/qrcode do the crypto and the QR rendering; everything here is just the shape the
product needs on top of them: a provisioning step that does not gate login until it is
proven working, and one-time backup codes stored the same way passwords are.
"""
import base64
import io
import json
import secrets

import pyotp
import qrcode
from passlib.context import CryptContext

from config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

BACKUP_CODE_COUNT = 10


def generate_secret() -> str:
    return pyotp.random_base32()


def provisioning_qr_png_b64(secret: str, email: str) -> str:
    """A data: URI the frontend can drop straight into an <img src>."""
    uri = pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=settings.FROM_NAME)
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def verify_code(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    # valid_window=1 tolerates the code from the previous/next 30s step, which a phone clock
    # a few seconds off would otherwise fail on every single time.
    return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)


def generate_backup_codes() -> tuple[list[str], str]:
    """Returns (plaintext codes to show ONCE, the hashed JSON blob to store)."""
    codes = ["-".join([secrets.token_hex(2), secrets.token_hex(2)]) for _ in range(BACKUP_CODE_COUNT)]
    hashed = [pwd_context.hash(c) for c in codes]
    return codes, json.dumps(hashed)


def consume_backup_code(stored_json: str | None, code: str) -> str | None:
    """Checks `code` against the stored hashes; returns the UPDATED json blob (with that
    code removed) if it matched, or None if it didn't — codes are single-use, so a match
    is consumed on the spot rather than merely checked."""
    if not stored_json or not code:
        return None
    hashes = json.loads(stored_json)
    code = code.strip().replace(" ", "")
    for h in hashes:
        if pwd_context.verify(code, h):
            hashes.remove(h)
            return json.dumps(hashes)
    return None

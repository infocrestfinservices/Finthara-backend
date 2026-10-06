import logging
from datetime import datetime, timedelta
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from config import settings

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

_MISSING_SECRET = ("SECRET_KEY is not set. Add it to backend/.env locally, or to the app's "
                   "environment variables on DigitalOcean, then restart the server.")
if not (settings.SECRET_KEY or "").strip():
    logger.error("auth_service: %s", _MISSING_SECRET)


def _secret() -> str:
    """The token-signing key. Refuses to work without one rather than signing with a
    guessable default."""
    key = (settings.SECRET_KEY or "").strip()
    if not key:
        raise RuntimeError(_MISSING_SECRET)
    return key


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def validate_password_strength(password: str) -> str:
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    return password


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, _secret(), algorithm=settings.ALGORITHM)


def create_reset_token(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(minutes=30)
    data = {"user_id": user_id, "exp": expire, "type": "reset"}
    return jwt.encode(data, _secret(), algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, _secret(), algorithms=[settings.ALGORITHM])
    except JWTError:
        return None
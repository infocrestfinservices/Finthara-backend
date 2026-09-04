from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional

from services.auth_service import validate_password_strength


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: Optional[str] = None

    @field_validator("password")
    @classmethod
    def _validate_password(cls, value: str) -> str:
        return validate_password_strength(value)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    email: str
    full_name: Optional[str] = None
    requires_2fa: bool = False


class TwoFactorChallengeResponse(BaseModel):
    requires_2fa: bool = True
    challenge_token: str


class TwoFactorLoginRequest(BaseModel):
    challenge_token: str
    code: str


class UserResponse(BaseModel):
    id: int
    email: str
    full_name: Optional[str] = None
    plan: Optional[str] = None
    is_admin: bool = False
    theme_preference: str = "system"
    avatar_url: Optional[str] = None

    class Config:
        from_attributes = True


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    # The SAME answer whether or not that address has an account. Anything that differs —
    # a 404, a different wording, even a different response time — turns this endpoint into
    # a way to ask "is this person a customer of yours?".
    message: str
    # Only ever populated on a development machine with no email provider configured, so
    # local testing is not blocked. Never set in production; see the endpoint.
    dev_reset_token: Optional[str] = None


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _validate_password(cls, value: str) -> str:
        return validate_password_strength(value)


class RegisterResponse(BaseModel):
    message: str
    # Dev-only: populated ONLY when SMTP is not configured, so the UI can show
    # the code instead of leaving you stuck waiting for an email that never sends.
    dev_otp: Optional[str] = None


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    otp: str


class ResendOtpRequest(BaseModel):
    email: EmailStr

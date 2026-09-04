from typing import Optional
from pydantic import BaseModel, EmailStr, field_validator

from services.auth_service import validate_password_strength


class ProfileResponse(BaseModel):
    id: int
    email: str
    is_verified: bool
    pending_email: Optional[str] = None
    full_name: Optional[str] = None
    phone: Optional[str] = None
    avatar_url: Optional[str] = None
    plan: Optional[str] = None
    theme_preference: str = "system"
    notify_email: bool = True
    totp_enabled: bool = False
    is_admin: bool = False

    class Config:
        from_attributes = True


class UpdateBasicInfoRequest(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None


class AvatarUploadRequest(BaseModel):
    # A data: URI (e.g. "data:image/png;base64,...") built client-side from the chosen file.
    data_url: str


class ChangeEmailRequest(BaseModel):
    new_email: EmailStr
    current_password: str


class VerifyEmailChangeRequest(BaseModel):
    otp: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _validate_password(cls, value: str) -> str:
        return validate_password_strength(value)


class NotificationPrefsRequest(BaseModel):
    notify_email: bool


class ThemePrefRequest(BaseModel):
    theme: str  # "light" | "dark" | "system"

    @field_validator("theme")
    @classmethod
    def _validate_theme(cls, value: str) -> str:
        if value not in ("light", "dark", "system"):
            raise ValueError("theme must be 'light', 'dark' or 'system'")
        return value


class TwoFactorSetupResponse(BaseModel):
    qr_code: str          # data: URI
    secret: str            # shown once, for manual entry
    backup_codes: list[str]  # shown once


class TwoFactorConfirmRequest(BaseModel):
    code: str


class TwoFactorDisableRequest(BaseModel):
    current_password: str


class DeleteAccountRequest(BaseModel):
    current_password: str

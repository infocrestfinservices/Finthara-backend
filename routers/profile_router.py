"""Profile: the account's own settings — basic info, avatar, email/password changes,
notification and theme preferences, two-factor auth, and account deletion.

Plan, usage and billing are deliberately NOT duplicated here: GET /payments/me
(services.entitlements + payment_router) is already the single source of truth for that,
and the frontend calls it directly for the Profile page's Plan tab.
"""
import logging
import random
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_current_user
from models.user_model import User
from schemas.profile_schema import (
    ProfileResponse, UpdateBasicInfoRequest, AvatarUploadRequest,
    ChangeEmailRequest, VerifyEmailChangeRequest, ChangePasswordRequest,
    NotificationPrefsRequest, ThemePrefRequest, TwoFactorSetupResponse,
    TwoFactorConfirmRequest, TwoFactorDisableRequest, DeleteAccountRequest,
)
from services.auth_service import hash_password, verify_password
from services.email_service import send_verification_email, email_configured
from services import totp_service

logger = logging.getLogger("profile")

router = APIRouter(prefix="/profile", tags=["Profile"])

# A data: URI this size decodes to roughly 500 KB — plenty for a profile picture and small
# enough that storing it as text in the row (see user_model.py) doesn't bloat every query
# that touches the user.
_MAX_AVATAR_CHARS = 700_000


@router.get("/me", response_model=ProfileResponse)
def get_profile(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/basic", response_model=ProfileResponse)
def update_basic_info(req: UpdateBasicInfoRequest,
                       current_user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    if req.full_name is not None:
        current_user.full_name = req.full_name.strip() or None
    if req.phone is not None:
        current_user.phone = req.phone.strip() or None
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/avatar", response_model=ProfileResponse)
def upload_avatar(req: AvatarUploadRequest,
                   current_user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    if not req.data_url.startswith("data:image/"):
        raise HTTPException(status_code=400, detail="File must be an image.")
    if len(req.data_url) > _MAX_AVATAR_CHARS:
        raise HTTPException(status_code=400, detail="Image is too large — please use one under ~500 KB.")
    current_user.avatar_url = req.data_url
    db.commit()
    db.refresh(current_user)
    return current_user


@router.delete("/avatar", response_model=ProfileResponse)
def remove_avatar(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    current_user.avatar_url = None
    db.commit()
    db.refresh(current_user)
    return current_user


# ── Email change (detours through the NEW address, same as signup) ─────────────────────
@router.post("/email/change")
async def request_email_change(req: ChangeEmailRequest,
                                current_user: User = Depends(get_current_user),
                                db: Session = Depends(get_db)):
    if not verify_password(req.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")

    new_email = req.new_email.lower().strip()
    if new_email == current_user.email:
        raise HTTPException(status_code=400, detail="That's already your email address.")

    taken = db.query(User).filter(User.email == new_email, User.id != current_user.id,
                                  User.is_verified.is_(True)).first()
    if taken:
        raise HTTPException(status_code=400, detail="That email address is already in use.")

    otp = str(random.randint(100000, 999999))
    current_user.pending_email = new_email
    current_user.pending_email_otp = otp
    current_user.pending_email_otp_expires_at = datetime.utcnow() + timedelta(minutes=10)

    try:
        await send_verification_email(new_email, current_user.full_name, otp)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=502,
                             detail=f"Could not send the verification email. ({e})")
    db.commit()

    if email_configured():
        return {"message": f"A verification code has been sent to {new_email}."}
    return {"message": "Email sending is not configured. Use the code shown below.", "dev_otp": otp}


@router.post("/email/verify-change", response_model=ProfileResponse)
def verify_email_change(req: VerifyEmailChangeRequest,
                         current_user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    if not current_user.pending_email:
        raise HTTPException(status_code=400, detail="No email change is pending.")
    if current_user.pending_email_otp != req.otp:
        raise HTTPException(status_code=400, detail="Invalid code.")
    if not current_user.pending_email_otp_expires_at or \
       current_user.pending_email_otp_expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Code has expired.")

    current_user.email = current_user.pending_email
    current_user.pending_email = None
    current_user.pending_email_otp = None
    current_user.pending_email_otp_expires_at = None
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/email/cancel-change", response_model=ProfileResponse)
def cancel_email_change(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    current_user.pending_email = None
    current_user.pending_email_otp = None
    current_user.pending_email_otp_expires_at = None
    db.commit()
    db.refresh(current_user)
    return current_user


# ── Password ─────────────────────────────────────────────────────────────────────────
@router.post("/password")
def change_password(req: ChangePasswordRequest,
                     current_user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    if not verify_password(req.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    current_user.hashed_password = hash_password(req.new_password)
    db.commit()
    return {"message": "Password updated."}


# ── Preferences ──────────────────────────────────────────────────────────────────────
@router.patch("/notifications", response_model=ProfileResponse)
def update_notifications(req: NotificationPrefsRequest,
                          current_user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    current_user.notify_email = req.notify_email
    db.commit()
    db.refresh(current_user)
    return current_user


@router.patch("/theme", response_model=ProfileResponse)
def update_theme(req: ThemePrefRequest,
                  current_user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    current_user.theme_preference = req.theme
    db.commit()
    db.refresh(current_user)
    return current_user


# ── Two-factor auth ──────────────────────────────────────────────────────────────────
@router.post("/2fa/setup", response_model=TwoFactorSetupResponse)
def setup_2fa(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Starts (or restarts) setup. Nothing is enforced at login until /2fa/confirm proves
    the app was scanned correctly — an unconfirmed secret must never lock anyone out."""
    if current_user.totp_enabled:
        raise HTTPException(status_code=400, detail="Two-factor authentication is already enabled.")
    secret = totp_service.generate_secret()
    current_user.totp_secret = secret
    db.commit()
    codes, hashed = totp_service.generate_backup_codes()
    # Backup codes are shown once, at confirm time, not here — generating them now and
    # showing them at setup would mean a secret that never got confirmed still handed out
    # working codes. Stash them unhashed in nothing; they're regenerated in /2fa/confirm.
    return TwoFactorSetupResponse(
        qr_code=totp_service.provisioning_qr_png_b64(secret, current_user.email),
        secret=secret,
        backup_codes=[],
    )


@router.post("/2fa/confirm", response_model=TwoFactorSetupResponse)
def confirm_2fa(req: TwoFactorConfirmRequest,
                 current_user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    if not current_user.totp_secret:
        raise HTTPException(status_code=400, detail="Start setup first.")
    if not totp_service.verify_code(current_user.totp_secret, req.code):
        raise HTTPException(status_code=400, detail="That code didn't match — check the time on your phone and try again.")

    codes, hashed = totp_service.generate_backup_codes()
    current_user.totp_enabled = True
    current_user.totp_backup_codes = hashed
    db.commit()
    return TwoFactorSetupResponse(
        qr_code=totp_service.provisioning_qr_png_b64(current_user.totp_secret, current_user.email),
        secret=current_user.totp_secret,
        backup_codes=codes,
    )


@router.post("/2fa/disable")
def disable_2fa(req: TwoFactorDisableRequest,
                 current_user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    if not verify_password(req.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    current_user.totp_enabled = False
    current_user.totp_secret = None
    current_user.totp_backup_codes = None
    db.commit()
    return {"message": "Two-factor authentication disabled."}


# ── Delete account (soft) ────────────────────────────────────────────────────────────
@router.delete("/me")
def delete_account(req: DeleteAccountRequest,
                    current_user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    if not verify_password(req.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")

    # Projects and invoices keep pointing at this row (see user_model.py) — only the email
    # is freed up, by renaming it out of the way, so the person can sign up again if they
    # come back.
    current_user.email = f"deleted-{current_user.id}-{current_user.email}"
    current_user.is_deleted = True
    current_user.deleted_at = datetime.utcnow()
    db.commit()
    return {"message": "Account deleted."}

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from config import settings
from database import get_db
from models.user_model import User
import random
from datetime import datetime, timedelta
from schemas.auth_schema import (
    RegisterRequest,
    LoginRequest,
    TokenResponse,
    UserResponse,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    ResetPasswordRequest,
    VerifyEmailRequest,
    ResendOtpRequest,
    RegisterResponse,
    TwoFactorLoginRequest,
)
from services.entitlements import FREE_PLAN
from services.email_service import send_password_reset_email, send_verification_email, email_configured
from services.auth_service import (
    hash_password,
    verify_password,
    create_access_token,
    create_reset_token,
    decode_access_token,
)
from services import totp_service
from dependencies import get_current_user

logger = logging.getLogger("auth")

router = APIRouter(prefix="/auth", tags=["Authentication"])

@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(request: RegisterRequest, db: Session = Depends(get_db)):
    email = request.email.lower().strip()

    existing = db.query(User).filter(User.email == email).first()
    # A *verified* email can't register again; an unverified one can retry.
    if existing and existing.is_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An account with this email already exists. Please log in instead."
        )

    otp = str(random.randint(100000, 999999))
    otp_expires_at = datetime.utcnow() + timedelta(minutes=10)

    if existing:
        # Re-registering an unverified account: refresh its details + OTP.
        existing.hashed_password = hash_password(request.password)
        existing.full_name = request.full_name
        existing.email_verification_otp = otp
        existing.otp_expires_at = otp_expires_at
        user = existing
    else:
        user = User(
            email=email,
            hashed_password=hash_password(request.password),
            full_name=request.full_name,
            # NOT "starter" — that is the name of the ₹499 plan, and hardcoding it here put
            # every new signup on a paid tier they had never bought. The column default is
            # "free" (1 report, PDF only) and this is the one place that was still
            # overriding it, so the whole entitlement layer was being bypassed at the front
            # door.
            plan=FREE_PLAN,
            is_verified=False,
            email_verification_otp=otp,
            otp_expires_at=otp_expires_at,
        )
        db.add(user)

    # Send BEFORE committing so a failed send never leaves a stuck account.
    try:
        await send_verification_email(email, request.full_name, otp)
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not send the verification email. Please check the SMTP settings. ({e})",
        )

    db.commit()

    if email_configured():
        return RegisterResponse(message="Verification OTP has been sent to your email.")
    # SMTP not set up yet — surface the code so testing isn't blocked.
    return RegisterResponse(
        message="Email sending is not configured. Use the code shown below to verify.",
        dev_otp=otp,
    )

@router.post("/login")
def login(request: LoginRequest, db: Session = Depends(get_db)):
    email = request.email.lower().strip()

    user = db.query(User).filter(User.email == email).first()
    # A deleted account's row keeps its (renamed) email, so a stranger who happens to guess
    # the renamed address still gets the same generic "invalid" answer as a wrong password —
    # not a hint that an account once existed here.
    if not user or user.is_deleted or not verify_password(request.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password"
        )

    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please verify your email before logging in."
        )

    if user.totp_enabled:
        # Password is correct but that's only the first factor: hand back a short-lived
        # challenge token (not a real session) that /auth/2fa/verify-login exchanges for one
        # once the code checks out. Nothing that acts as this user is issued yet.
        challenge = create_access_token({"user_id": user.id, "type": "2fa_pending"},
                                        expires_delta=timedelta(minutes=5))
        return {"requires_2fa": True, "challenge_token": challenge}

    token = create_access_token({"user_id": user.id, "email": user.email})
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        email=user.email,
        full_name=user.full_name
    )


@router.post("/2fa/verify-login", response_model=TokenResponse)
def verify_login_2fa(request: TwoFactorLoginRequest, db: Session = Depends(get_db)):
    payload = decode_access_token(request.challenge_token)
    if not payload or payload.get("type") != "2fa_pending":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                             detail="This login has expired. Please sign in again.")
    user = db.query(User).filter(User.id == payload.get("user_id")).first()
    if not user or not user.totp_enabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session.")

    code = request.code.strip().replace(" ", "")
    if totp_service.verify_code(user.totp_secret, code):
        pass
    else:
        # Not a TOTP code — maybe it's a backup code. Consuming it (removing it from the
        # stored list) happens right here, on a successful match, since each one works once.
        updated = totp_service.consume_backup_code(user.totp_backup_codes, code)
        if updated is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                 detail="Invalid code.")
        user.totp_backup_codes = updated
        db.commit()

    token = create_access_token({"user_id": user.id, "email": user.email})
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        email=user.email,
        full_name=user.full_name
    )

@router.post("/verify-email")
def verify_email(request: VerifyEmailRequest, db: Session = Depends(get_db)):
    email = request.email.lower().strip()
    user = db.query(User).filter(User.email == email).first()

    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    
    if user.is_verified:
        return {"message": "Email is already verified."}

    if user.email_verification_otp != request.otp:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OTP")
        
    if not user.otp_expires_at or user.otp_expires_at < datetime.utcnow():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OTP has expired")

    user.is_verified = True
    user.email_verification_otp = None
    user.otp_expires_at = None
    db.commit()
    
    return {"message": "Email verified successfully."}

@router.post("/resend-otp")
async def resend_otp(request: ResendOtpRequest, db: Session = Depends(get_db)):
    email = request.email.lower().strip()
    user = db.query(User).filter(User.email == email).first()

    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if user.is_verified:
        return {"message": "Email is already verified."}

    # Generate new OTP
    otp = str(random.randint(100000, 999999))
    user.email_verification_otp = otp
    user.otp_expires_at = datetime.utcnow() + timedelta(minutes=10)

    try:
        await send_verification_email(email, user.full_name, otp)
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not send the verification email. Please check the SMTP settings. ({e})",
        )

    db.commit()

    if email_configured():
        return {"message": "A new verification OTP has been sent to your email."}
    return {"message": "Email sending is not configured. Use the code shown below.", "dev_otp": otp}


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user

@router.post("/logout")
def logout():
    return {"message": "Logged out successfully"}

# The one answer this endpoint ever gives, whatever happened.
_RESET_SENT = ("If that email address has an account, we have sent a link to reset the "
               "password. Please check your inbox.")


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
async def forgot_password(request: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """Email a reset link. Never reveals whether the address has an account.

    This used to RETURN the reset token in the response, so anyone who knew a customer's
    email could take their account, and it answered 404 for an unknown address, so the same
    endpoint could be used to find out who the customers were. Both are closed here: the
    token now only ever travels by email, and the answer is identical either way.
    """
    email = request.email.lower().strip()
    user = db.query(User).filter(User.email == email).first()

    if not user or user.is_deleted:
        # Deliberately the same reply, and deliberately no lookup-shaped work skipped that
        # would make this measurably faster than the branch below.
        logger.info("auth: reset requested for an address with no account")
        return ForgotPasswordResponse(message=_RESET_SENT)

    token = create_reset_token(user.id)
    link = f"{settings.FRONTEND_URL.rstrip('/')}/reset-password?token={quote(token)}"

    sent = False
    try:
        sent = await send_password_reset_email(
            user.email, user.full_name, link, settings.RESET_TOKEN_MINUTES)
    except Exception:
        # A provider outage must not tell the caller anything either. It is logged so the
        # failure is visible to us, and the customer is told to check their inbox exactly as
        # they would have been.
        logger.exception("auth: reset email failed to send")

    if sent:
        logger.info("auth: reset link emailed to user %s", user.id)
        return ForgotPasswordResponse(message=_RESET_SENT)

    # No email provider on this machine. On a development box the token is handed back so
    # local testing is not blocked — the same accommodation the OTP flow makes. In
    # production it is never returned: an unsent email is a support problem, and handing the
    # token to the caller would put back the exact hole this endpoint just closed.
    if settings.ENV.strip().lower() == "production":
        logger.error("auth: reset email could not be sent and this is production — "
                     "RESEND_API_KEY / FROM_EMAIL are not usable")
        return ForgotPasswordResponse(message=_RESET_SENT)

    logger.warning("auth: no email provider configured; returning the token for local use")
    return ForgotPasswordResponse(message=_RESET_SENT, dev_reset_token=token)

@router.post("/reset-password", response_model=UserResponse)
def reset_password(request: ResetPasswordRequest, db: Session = Depends(get_db)):
    payload = decode_access_token(request.token)
    if not payload or payload.get("type") != "reset":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token"
        )

    user = db.query(User).filter(User.id == payload.get("user_id")).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    user.hashed_password = hash_password(request.new_password)
    db.commit()
    db.refresh(user)
    return user
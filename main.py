import secrets

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from config import settings
from database import Base, engine, ensure_columns, ensure_nullable, ensure_index  # Auto-table creation import

from routers.industry_router import router as industry_router
from routers.country_router import router as country_router
from routers.purpose_router import router as purpose_router
from routers.ai_router import router as ai_router
from routers.project_router import router as project_router
from routers.analysis_router import router as analysis_router
from routers.auth_router import router as auth_router
from routers.generation_router import router as generation_router
from routers.templates_router import router as templates_router
from routers.bank_loan_router import router as bank_loan_router
from routers.payment_router import router as payment_router
from routers.paypal_router import router as paypal_router
from routers.admin_router import router as admin_router
from routers.invoice_router import router as invoice_router
from routers.profile_router import router as profile_router
from routers.team_router import router as team_router
from routers.engine_test_router import router as engine_test_router  # dev only

IS_PRODUCTION = settings.ENV.strip().lower() == "production"

# create_all() below only creates a table for a model class that has actually been imported
# somewhere by this point — every OTHER model gets pulled in eagerly by some router's
# top-level `from models.x import Y` (payment_model via payment_router, invoice_model via
# invoice_router, ...), but nothing imports coupon_model that way; every reference to it is a
# lazy, inside-a-function import (admin_router's coupon endpoints, services/coupons.py). On an
# already-provisioned database this is invisible — the table already exists — which is exactly
# what let it go unnoticed; a genuinely FRESH database would create every table except this
# one, and the first coupon ever applied would fail with "no such table: coupons".
from models.coupon_model import Coupon, CouponRedemption  # noqa: F401
# Team seats: same reasoning — every reference to these is inside a function/router body,
# so create_all() only learns about the tables if they are imported here too. team_router
# does import them eagerly, but keep it explicit and independent of router wiring.
from models.company_model import CompanyUser, CompanyInvitation  # noqa: F401
from models.audit_log_model import AuditLog  # noqa: F401

# Create all database tables on startup (Neon / Supabase Fix)
Base.metadata.create_all(bind=engine)

# Columns added to models after their table already existed — create_all above never alters
# an existing table, so these need an explicit ALTER TABLE. See database.ensure_columns.
ensure_columns("users", {
    "phone": "VARCHAR",
    "avatar_url": "VARCHAR",
    "theme_preference": "VARCHAR NOT NULL DEFAULT 'system'",
    "notify_email": "BOOLEAN NOT NULL DEFAULT true",
    "pending_email": "VARCHAR",
    "pending_email_otp": "VARCHAR",
    "pending_email_otp_expires_at": "TIMESTAMP",
    "totp_secret": "VARCHAR",
    "totp_enabled": "BOOLEAN NOT NULL DEFAULT false",
    "totp_backup_codes": "VARCHAR",
    "is_deleted": "BOOLEAN NOT NULL DEFAULT false",
    "deleted_at": "TIMESTAMP",
})
ensure_columns("payments", {
    "gateway": "VARCHAR NOT NULL DEFAULT 'razorpay'",
    "paypal_order_id": "VARCHAR",
    "paypal_capture_id": "VARCHAR",
})
# razorpay_order_id was NOT NULL when every payment was a Razorpay payment; a PayPal row has
# no Razorpay order at all. See models/payment_model.py.
ensure_nullable("payments", "razorpay_order_id")
ensure_index("ix_payments_paypal_order_id", "payments", "paypal_order_id", unique=True)
ensure_columns("webhook_events", {
    "gateway": "VARCHAR NOT NULL DEFAULT 'razorpay'",
})
ensure_index("ix_webhook_events_gateway", "webhook_events", "gateway")

app = FastAPI(
    title="AI Feasibility Study & Project Report Generator",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Parse CORS origins safely
if isinstance(settings.cors_origins, str):
    cors_origins_list = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
else:
    cors_origins_list = list(settings.cors_origins)

# Fallback so server never crashes on CORS mismatch
if not cors_origins_list:
    cors_origins_list = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if not IS_PRODUCTION else cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

app.include_router(industry_router)
app.include_router(country_router)
app.include_router(purpose_router)
app.include_router(ai_router)
app.include_router(project_router)
app.include_router(analysis_router)
app.include_router(auth_router)
app.include_router(generation_router)
app.include_router(templates_router)
app.include_router(bank_loan_router)
app.include_router(payment_router)
app.include_router(paypal_router)
app.include_router(admin_router)
app.include_router(invoice_router)
app.include_router(profile_router)
app.include_router(team_router)

if not IS_PRODUCTION:
    app.include_router(engine_test_router)


@app.on_event("startup")
def _register_folder_templates():
    import logging
    try:
        from services.template_upload_service import scan_templates_dir
        added = scan_templates_dir()
        ok = [a for a in added if a.get("template_id")]
        if added:
            logging.getLogger("templates").info(
                "startup: registered %d folder template(s); %d skipped/errored",
                len(ok), len(added) - len(ok))
    except Exception:
        logging.getLogger("templates").warning("startup template scan failed", exc_info=True)


@app.get("/")
def home():
    return {"message": "Backend is working"}

@app.get("/health")
def health():
    return {"status": "healthy"}


_basic = HTTPBasic(auto_error=False)


def _docs_guard(credentials: HTTPBasicCredentials = Depends(_basic)):
    if not IS_PRODUCTION:
        return True
    if not settings.DOCS_PASSWORD:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    
    ok_user = secrets.compare_digest((credentials.username if credentials else ""),
                                     settings.DOCS_USER)
    ok_pass = secrets.compare_digest((credentials.password if credentials else ""),
                                     settings.DOCS_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authorised",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True


@app.get("/openapi.json", include_in_schema=False)
def openapi_schema(_=Depends(_docs_guard)):
    return app.openapi()


@app.get("/docs", include_in_schema=False)
def swagger_ui(_=Depends(_docs_guard)):
    return get_swagger_ui_html(openapi_url="/openapi.json", title="API docs")
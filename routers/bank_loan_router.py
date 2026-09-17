"""
bank_loan_router.py

HTTP surface for the Bank Loan report pipeline.

  POST /bank-loan/generate  -> Generic Pipeline -> (Bank Loan executor) -> .xlsx

The router validates the request and delegates to the Generic Pipeline. It no longer
knows anything about Bank Loan orchestration: it simply calls run_report_pipeline,
which resolves the PurposeConfig via the Purpose Catalog, selects the Bank Loan
executor, and delegates to the existing services/bank_loan_pipeline_service exactly as
before. No AI, template, Excel, orchestration or file-writing logic here.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from dependencies import get_admin_user
from services.generic_pipeline_service import run_report_pipeline

logger = logging.getLogger("bank_loan")

# STAFF ONLY. This route ran the whole pipeline — AI call, workbook fill, LibreOffice
# recalculation, and a retry on a low DSCR, so up to two AI calls per request — with no
# login, no plan check and no rate limit. That was right when it was written: there were no
# accounts yet. It stopped being right the moment reports became something people pay for,
# because it is a second door to the product that walks straight past every plan limit on
# /generate/. On a public domain it is an open tab on the AI bill.
#
# Kept rather than deleted because it is how the engine is tested against a deployment. The
# admin dependency is what makes that safe: staff can still call it from the live URL, and
# for everyone else the address answers 404 — it does not even confirm the route exists.
router = APIRouter(prefix="/bank-loan", tags=["Bank Loan"],
                   dependencies=[Depends(get_admin_user)])


class BankLoanRequest(BaseModel):
    industry: str = Field(..., min_length=1, description="e.g. 'Manufacturing'")
    purpose: str = Field("Bank Loan", min_length=1)
    user_details: str = Field(..., min_length=1,
                              description="Free-form description of the business and the facility sought")


@router.post("/generate")
def generate_bank_loan_report(req: BankLoanRequest):
    """Generate a complete, recalculated Bank Loan workbook and return the
    pipeline's result dict verbatim.

    Delegates to the Generic Pipeline, which routes 'Bank Loan' to the existing Bank
    Loan pipeline unchanged — so the response is byte-for-byte what it was before."""
    try:
        return run_report_pipeline(
            industry=req.industry,
            purpose=req.purpose,
            user_details=req.user_details,
        )
    except ValueError as e:
        # unparseable AI JSON, or the template is not registered. Not 502 — App Platform's
        # edge substitutes its own generic error page for a 5xx from the app.
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.exception("bank-loan generation failed")
        raise HTTPException(status_code=409, detail=f"Report generation failed: {e}")

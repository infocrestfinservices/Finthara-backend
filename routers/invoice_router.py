"""The customer's own invoices.

Every route resolves the invoice through `_owned`, which filters on the signed-in user's id
and answers **404** when it does not match — the same rule as `get_owned_project`. A 403 would
confirm that invoice number exists and belongs to someone, which is exactly what an invoice
number must not reveal: they are sequential, so anyone could walk the series and learn how
many customers there are and when each of them paid.
"""
import logging
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_current_user
from models.invoice_model import Invoice
from models.user_model import User
from services import invoice_pdf

logger = logging.getLogger("invoices")
router = APIRouter(prefix="/invoices", tags=["Invoices"])

PDF_MIME = "application/pdf"


def _owned(db: Session, invoice_id: int, user: User) -> Invoice:
    inv = (db.query(Invoice)
             .filter(Invoice.id == invoice_id, Invoice.user_id == user.id)
             .first())
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return inv


def _as_dict(inv: Invoice) -> dict:
    is_tax = bool(inv.supplier_gstin)
    return {
        "id": inv.id,
        "invoice_number": inv.invoice_number,
        "document_type": "Tax Invoice" if is_tax else "Bill of Supply",
        "issued_at": inv.issued_at.isoformat() if inv.issued_at else None,
        "status": inv.status,
        "customer": {"name": inv.customer_name, "email": inv.customer_email},
        "supplier": {
            "name": inv.supplier_name, "address": inv.supplier_address,
            "email": inv.supplier_email, "gstin": inv.supplier_gstin,
        },
        "plan": inv.plan,
        "description": inv.description,
        "sac_code": inv.sac_code,
        "period_start": inv.period_start.isoformat() if inv.period_start else None,
        "period_end": inv.period_end.isoformat() if inv.period_end else None,
        "currency": inv.currency,
        "gross": inv.gross,
        "discount": inv.discount,
        "coupon_code": inv.coupon_code,
        "taxable_value": inv.taxable_value,
        "tax_rate": inv.tax_rate,
        "tax_total": round((inv.cgst or 0) + (inv.sgst or 0) + (inv.igst or 0), 2),
        "cgst": inv.cgst, "sgst": inv.sgst, "igst": inv.igst,
        "total": inv.total,
        "amount_paid": inv.amount_paid,
        "amount_due": inv.amount_due,
        "place_of_supply": inv.place_of_supply,
    }


@router.get("")
def list_invoices(db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user),
                  limit: int = Query(50, le=200), offset: int = 0):
    """This user's invoices, newest first."""
    q = db.query(Invoice).filter(Invoice.user_id == current_user.id)
    total = q.count()
    rows = q.order_by(Invoice.issued_at.desc(), Invoice.id.desc()) \
            .limit(limit).offset(offset).all()
    return {
        "total": total, "limit": limit, "offset": offset,
        "invoices": [{
            "id": i.id,
            "invoice_number": i.invoice_number,
            "issued_at": i.issued_at.isoformat() if i.issued_at else None,
            "plan": i.plan,
            "description": i.description,
            "currency": i.currency,
            "total": i.total,
            "amount_due": i.amount_due,
            "status": i.status,
        } for i in rows],
    }


@router.get("/{invoice_id}")
def get_invoice(invoice_id: int, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    return _as_dict(_owned(db, invoice_id, current_user))


@router.get("/{invoice_id}/pdf")
def download_invoice(invoice_id: int, db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    inv = _owned(db, invoice_id, current_user)
    try:
        data = invoice_pdf.render(inv)
    except Exception as e:
        logger.exception("invoices: PDF render failed for %s", inv.invoice_number)
        raise HTTPException(status_code=409, detail=f"Could not build the PDF: {e}")
    return StreamingResponse(
        BytesIO(data), media_type=PDF_MIME,
        headers={"Content-Disposition": f'attachment; filename="{inv.invoice_number}.pdf"'},
    )

"""GST on what we sell — the one place that decides it.

Prices on the pricing page are GST-EXCLUSIVE: the plan price (after any coupon) is the
TAXABLE VALUE, and 18% GST is added on top. ₹1,999 is charged as ₹2,358.82.

Which tax applies depends on where the customer is (the place of supply, for a B2C sale of
an online service, is the customer's state):
  - same state as the supplier  -> CGST 9% + SGST 9%
  - a different state           -> IGST 18%
The supplier's state is the first two digits of its GSTIN, so nothing else has to be kept
in step with it. A business customer who gives their GSTIN is placed by THAT GSTIN's state.

All of this switches on only when COMPANY_GSTIN is set. Until then the business is not
registered: no GST is charged and the invoice is a Bill of Supply.

PayPal payments (USD, customers outside India) carry no GST — export of services.
"""
from __future__ import annotations

import re

from config import settings

# GST state / union-territory codes (the first two digits of a GSTIN).
STATES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan",
    "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh",
    "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura", "17": "Meghalaya",
    "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh",
    "38": "Ladakh", "97": "Other Territory",
}

_GSTIN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def registered() -> bool:
    return bool((settings.COMPANY_GSTIN or "").strip())


def rate() -> float:
    return float(settings.GST_RATE or 0) if registered() else 0.0


def company_state() -> str | None:
    """The supplier's state code, from its own GSTIN."""
    code = (settings.COMPANY_GSTIN or "").strip()[:2]
    return code if code in STATES else None


def state_code(value) -> str | None:
    """A state code from a code ("23"), a name ("Madhya Pradesh"), or None."""
    v = str(value or "").strip()
    if not v:
        return None
    if v.zfill(2) in STATES:
        return v.zfill(2)
    folded = re.sub(r"[^a-z]", "", v.lower())
    for code, name in STATES.items():
        if re.sub(r"[^a-z]", "", name.lower()) == folded:
            return code
    return None


def state_label(code) -> str | None:
    return f"{STATES[code]} ({code})" if code in STATES else None


def clean_gstin(value) -> str | None:
    """An uppercase, well-formed GSTIN with a real state code, or None."""
    v = re.sub(r"\s", "", str(value or "")).upper()
    return v if _GSTIN.match(v) and v[:2] in STATES else None


def compute(taxable: float, customer_state: str | None) -> dict:
    """Split for a taxable value: {taxable_value, tax_rate, cgst, sgst, igst, total}.

    Not registered -> no tax at all. Registered -> 18% on top, as CGST+SGST when the
    customer is in the supplier's state, otherwise IGST.
    """
    taxable = round(float(taxable or 0), 2)
    r = rate()
    if not r:
        return {"taxable_value": taxable, "tax_rate": 0.0,
                "cgst": 0.0, "sgst": 0.0, "igst": 0.0, "total": taxable}
    tax = round(taxable * r, 2)
    if customer_state and customer_state == company_state():
        cgst = round(tax / 2, 2)
        return {"taxable_value": taxable, "tax_rate": r, "cgst": cgst,
                "sgst": round(tax - cgst, 2), "igst": 0.0, "total": round(taxable + tax, 2)}
    return {"taxable_value": taxable, "tax_rate": r, "cgst": 0.0, "sgst": 0.0,
            "igst": tax, "total": round(taxable + tax, 2)}

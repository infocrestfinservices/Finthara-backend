"""The invoice as a PDF, drawn with reportlab.

reportlab because it is pure Python with no system dependencies. The alternatives all drag
something heavy onto the server: WeasyPrint needs cairo and pango, anything HTML-to-PDF needs
a browser, and the existing Word-to-PDF path needs LibreOffice — which is already a
deployment burden for the reports and would be absurd for a one-page receipt.

The layout follows the reference: the document type and the amount lead, the two parties sit
side by side, then the line items, then a right-aligned summary ending on the amount due.
Everything is positioned from the same margins and a single cursor, so a longer address or a
wrapped description pushes what follows down instead of overlapping it.
"""
from __future__ import annotations

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

# Brand blue, the same one the app uses for its primary.
BRAND = colors.HexColor("#1B3A6B")
INK = colors.HexColor("#12181F")
MUTED = colors.HexColor("#6B7684")
RULE = colors.HexColor("#DDE3EA")

PAGE_W, PAGE_H = A4
M = 18 * mm                    # margin
COL2 = PAGE_W / 2 + 4 * mm     # where the right-hand column starts


def _money(value, currency="INR") -> str:
    # No rupee glyph: the built-in Helvetica has no ₹, and a missing glyph prints as a black
    # box on the customer's copy. The ISO code is unambiguous and always renders.
    return f"{float(value or 0):,.2f} {currency}"


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    words, lines, line = (text or "").split(), [], ""
    for w in words:
        trial = f"{line} {w}".strip()
        if pdfmetrics.stringWidth(trial, font, size) <= width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = w
    if line:
        lines.append(line)
    return lines or [""]


def _block(c, x, y, lines, *, font="Helvetica", size=9.5, leading=13, colour=INK, width=None):
    """Draw a stack of lines, wrapping if a width is given. Returns the new y."""
    c.setFillColor(colour)
    c.setFont(font, size)
    for raw in lines:
        if raw is None:
            continue
        for part in (_wrap(str(raw), font, size, width) if width else [str(raw)]):
            c.drawString(x, y, part)
            y -= leading
    return y


def render(inv) -> bytes:
    """An Invoice row -> PDF bytes."""
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(inv.invoice_number)

    is_tax = bool(inv.supplier_gstin)
    title = "TAX INVOICE" if is_tax else "BILL OF SUPPLY"
    y = PAGE_H - M

    # ── header: type, amount, brand ───────────────────────────────────────────
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 8.5)
    c.drawString(M, y, title)

    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 25)
    c.drawString(M, y - 26, _money(inv.total, inv.currency))

    c.setFillColor(BRAND)
    c.setFont("Helvetica-Bold", 15)
    c.drawRightString(PAGE_W - M, y - 4, inv.supplier_name)
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 8.5)
    c.drawRightString(PAGE_W - M, y - 18, "Bank & investor-ready project reports")

    y -= 46
    c.setStrokeColor(RULE)
    c.setLineWidth(0.8)
    c.line(M, y, PAGE_W - M, y)
    y -= 18

    # ── the two parties, side by side ─────────────────────────────────────────
    label_y = y
    c.setFillColor(MUTED); c.setFont("Helvetica", 8)
    c.drawString(M, label_y, "TO")
    c.drawString(COL2, label_y, "FROM")
    y -= 14

    left_end = _block(c, M, y, [
        getattr(inv, "customer_company", None) or None,
        inv.customer_name or inv.customer_email,
        inv.customer_email if inv.customer_name else None,
        f"GSTIN {inv.customer_gstin}" if getattr(inv, "customer_gstin", None) else None,
    ], width=COL2 - M - 12 * mm)

    right_end = _block(c, COL2, y, [
        inv.supplier_name,
        inv.supplier_address or None,
        inv.supplier_email or None,
        f"GSTIN {inv.supplier_gstin}" if is_tax else None,
    ], width=PAGE_W - M - COL2)

    y = min(left_end, right_end) - 10

    # ── the reference numbers ─────────────────────────────────────────────────
    c.setFillColor(MUTED); c.setFont("Helvetica", 8)
    c.drawString(M, y, "INVOICE NUMBER")
    c.drawString(M + 62 * mm, y, "ISSUE DATE")
    y -= 13
    c.setFillColor(INK); c.setFont("Helvetica-Bold", 10)
    c.drawString(M, y, inv.invoice_number)
    c.setFont("Helvetica", 10)
    c.drawString(M + 62 * mm, y, inv.issued_at.strftime("%d %b %Y") if inv.issued_at else "—")
    y -= 24

    # ── line items ────────────────────────────────────────────────────────────
    c.setStrokeColor(RULE)
    c.line(M, y, PAGE_W - M, y)
    y -= 13
    c.setFillColor(MUTED); c.setFont("Helvetica", 8)
    c.drawString(M, y, "DESCRIPTION")
    if is_tax and inv.sac_code:
        c.drawString(COL2 + 8 * mm, y, "SAC")
    c.drawRightString(PAGE_W - M, y, "AMOUNT")
    y -= 8
    c.line(M, y, PAGE_W - M, y)
    y -= 16

    desc_lines = _wrap(inv.description, "Helvetica", 9.5, COL2 - M + 6 * mm)
    c.setFillColor(INK); c.setFont("Helvetica", 9.5)
    for i, part in enumerate(desc_lines):
        c.drawString(M, y - i * 13, part)
    if is_tax and inv.sac_code:
        c.drawString(COL2 + 8 * mm, y, inv.sac_code)
    c.drawRightString(PAGE_W - M, y, _money((inv.taxable_value or 0) + (inv.discount or 0),
                                            inv.currency))
    y -= 13 * len(desc_lines) + 8

    if inv.coupon_code and inv.discount:
        c.setFillColor(MUTED); c.setFont("Helvetica", 9)
        c.drawString(M, y, f"Coupon {inv.coupon_code} applied")
        c.drawRightString(PAGE_W - M, y, f"-{_money(inv.discount, inv.currency)}")
        y -= 15

    c.setStrokeColor(RULE)
    c.line(M, y, PAGE_W - M, y)
    y -= 18

    # ── summary, right aligned ────────────────────────────────────────────────
    LABEL_X = PAGE_W - M - 52 * mm

    def summary(label, value, *, bold=False, size=9.5, gap=15):
        nonlocal y
        c.setFillColor(INK if bold else MUTED)
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(LABEL_X, y, label)
        c.setFillColor(INK)
        c.drawRightString(PAGE_W - M, y, value)
        y -= gap

    if is_tax:
        summary("Taxable value", _money(inv.taxable_value, inv.currency))
        half = (inv.tax_rate or 0) * 100 / 2
        if inv.cgst or inv.sgst:
            summary(f"CGST {half:g}%", _money(inv.cgst or 0, inv.currency))
            summary(f"SGST {half:g}%", _money(inv.sgst or 0, inv.currency))
        elif inv.igst:
            summary(f"IGST {(inv.tax_rate or 0) * 100:g}%", _money(inv.igst, inv.currency))
    summary("Total", _money(inv.total, inv.currency))
    summary("Less amount paid", _money(inv.amount_paid, inv.currency))

    y -= 2
    c.setStrokeColor(RULE)
    c.line(LABEL_X, y + 8, PAGE_W - M, y + 8)
    y -= 6
    summary("Amount due", _money(inv.amount_due, inv.currency), bold=True, size=12, gap=20)

    # ── footer notes ──────────────────────────────────────────────────────────
    notes = []
    if not is_tax:
        # Required wording for a supplier who is not registered: without it the document
        # implies a registration that does not exist.
        notes.append(f"{inv.supplier_name} is not registered for GST. "
                     f"No tax has been charged on this supply.")
    if inv.place_of_supply:
        notes.append(f"Place of supply: {inv.place_of_supply}")
    if getattr(inv, "tax_note", None):
        notes.append(inv.tax_note)
    notes.append("This is a computer-generated document and needs no signature.")

    ny = M + 26
    c.setStrokeColor(RULE)
    c.line(M, ny + 16, PAGE_W - M, ny + 16)
    _block(c, M, ny, notes, size=8, leading=11, colour=MUTED, width=PAGE_W - 2 * M)

    c.showPage()
    c.save()
    return buf.getvalue()

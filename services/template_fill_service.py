"""
template_fill_service.py

The heart of the "report just like the sample" feature.

We take the chosen sample workbook and overwrite ONLY its input cells with the
user's answers, keeping EVERYTHING else byte-for-byte identical — cover-page
shapes/text boxes, chart styling, conditional formatting, VML drawings, VBA,
number formats, column widths, print layout, the lot.

Why not openpyxl? openpyxl loads a workbook into its own object model and
re-serialises on save, silently dropping whatever it doesn't model: drawing
shapes/text boxes (so cover & contents pages come out blank), chart color/style
parts, sparklines, etc. For designer-built financial models that destroys the
presentation. So instead we edit the .xlsx as what it actually is — a zip of XML
parts — rewriting just the worksheet cells we change and flipping the workbook's
"recalculate on load" flag. Every other zip entry is copied verbatim.

Excel recomputes all formulas on open (fullCalcOnLoad), so overwriting the input
cells cascades through the entire model exactly as the sample was built to.

Answers are keyed by "Sheet!Cell" (the `key` each schema field carries).
"""

import io
import logging
import re
import zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, unescape

from template_config import template_path
from services.template_introspect import load_schema

logger = logging.getLogger("template_fill")

# Parts every valid OOXML workbook must contain.
_REQUIRED_PARTS = ("[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels")


def field_key(sheet: str, cell: str) -> str:
    return f"{sheet}!{cell}"


def _validate_workbook(data: bytes, changed_parts) -> None:
    """Reopen the produced bytes and prove they form a valid workbook before we
    ever hand them to the client. Raises ValueError on any problem so the caller
    returns an error instead of a corrupt file.

    - the ZIP archive must be intact (testzip)
    - every required OOXML part must be present
    - every part we rewrote (worksheets + workbook.xml) must be well-formed XML
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError(f"produced bytes are not a valid zip: {e}")
    bad = zf.testzip()
    if bad is not None:
        raise ValueError(f"corrupt zip entry: {bad}")
    names = set(zf.namelist())
    missing = [p for p in _REQUIRED_PARTS if p not in names]
    if missing:
        raise ValueError(f"missing required OOXML parts: {missing}")
    for part in changed_parts:
        try:
            ET.fromstring(zf.read(part))
        except ET.ParseError as e:
            raise ValueError(f"rewrote {part} into malformed XML: {e}")


def _coerce(value, ftype: str):
    """Coerce an incoming answer to a cell value. Percents are stored as raw
    fractions (0.03), same as the template. Returns (kind, value) where kind is
    'num' or 'str', or None to skip."""
    if value is None or value == "":
        return None
    if ftype in ("number", "percent"):
        try:
            num = float(value)
            return ("num", int(num) if num.is_integer() else num)
        except (TypeError, ValueError):
            return ("str", str(value))
    return ("str", str(value))


def _sheet_name_to_path(zf: zipfile.ZipFile) -> dict:
    """Map each worksheet's display name -> its XML part path inside the zip."""
    wb_xml = zf.read("xl/workbook.xml").decode("utf-8")
    rels_xml = zf.read("xl/_rels/workbook.xml.rels").decode("utf-8")

    rid_to_target = {}
    for m in re.finditer(r"<Relationship\b[^>]*>", rels_xml):
        tag = m.group(0)
        rid = re.search(r'\bId="([^"]*)"', tag)
        tgt = re.search(r'\bTarget="([^"]*)"', tag)
        if rid and tgt:
            rid_to_target[rid.group(1)] = tgt.group(1)

    name_to_path = {}
    for m in re.finditer(r"<sheet\b[^>]*/?>", wb_xml):
        tag = m.group(0)
        name = re.search(r'\bname="([^"]*)"', tag)
        rid = re.search(r'\br:id="([^"]*)"', tag) or re.search(r'\br:ns\d*:id="([^"]*)"', tag)
        if not (name and rid):
            continue
        target = rid_to_target.get(rid.group(1))
        if not target:
            continue
        # Targets may be relative ("worksheets/sheet1.xml") or absolute
        # ("/xl/worksheets/sheet1.xml").
        if target.startswith("/"):
            path = target.lstrip("/")
        else:
            path = "xl/" + target.lstrip("./")
        name_to_path[unescape(name.group(1))] = path
    return name_to_path


def _cell_style_index(sheet_xml: str, cell_ref: str) -> str | None:
    """The `s` (style index) attribute of a cell, or None if it has none/doesn't exist."""
    m = re.search(r'<c\b[^>]*\br="%s"[^>]*?(?:/>|>)' % re.escape(cell_ref), sheet_xml)
    if not m:
        return None
    sm = re.search(r'\bs="([^"]*)"', m.group(0))
    return sm.group(1) if sm else None


def _set_cell_xml(sheet_xml: str, cell_ref: str, kind: str, value, style_index: str | None = None) -> tuple:
    """Overwrite one cell's value inside a worksheet XML string, preserving the
    cell's style (`s`) attribute — unless `style_index` is given, which borrows
    another cell's number format (see `style_overrides` on `fill_template`, used
    when the same input cell means a different kind of number for a different
    industry, e.g. a 0-1 fraction that must show as "%" rather than "₹/unit").
    Returns (new_xml, changed?)."""
    # Match the whole <c r="C7" ...>...</c> element (or self-closing <c .../>).
    pattern = re.compile(
        r'(<c\b[^>]*\br="%s"[^>]*?)(/>|>.*?</c>)' % re.escape(cell_ref),
        re.DOTALL,
    )

    def repl(m):
        opening = m.group(1)
        # Drop any existing type attribute; we set the type explicitly below.
        opening = re.sub(r'\s+t="[^"]*"', "", opening)
        if style_index is not None:
            if re.search(r'\bs="[^"]*"', opening):
                opening = re.sub(r'\bs="[^"]*"', f's="{style_index}"', opening)
            else:
                opening = f'{opening} s="{style_index}"'
        if kind == "str":
            return f'{opening} t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'
        return f"{opening}><v>{value}</v></c>"

    new_xml, n = pattern.subn(repl, sheet_xml, count=1)
    return new_xml, n > 0


def _set_full_calc_on_load(wb_xml: str) -> str:
    """Ensure Excel recomputes the whole model when the file is opened."""
    if "<calcPr" in wb_xml:
        def fix(m):
            attrs = m.group(1)
            if "fullCalcOnLoad" in attrs:
                attrs = re.sub(r'\s*fullCalcOnLoad="[^"]*"', "", attrs)
            return f'<calcPr{attrs} fullCalcOnLoad="1"/>'
        return re.sub(r"<calcPr\b(.*?)/>", fix, wb_xml, count=1, flags=re.DOTALL)
    # No calcPr present — insert one just before the closing tag.
    return wb_xml.replace("</workbook>", '<calcPr calcId="0" fullCalcOnLoad="1"/></workbook>')


def fill_template(purpose_key: str, template_id: str, answers: dict,
                  style_overrides: dict[str, str] | None = None) -> bytes:
    """Return .xlsx/.xlsm bytes for the template with `answers` written into its
    input cells and everything else preserved. `answers` maps "Sheet!Cell" -> value.

    `style_overrides` (optional) maps a target "Sheet!Cell" to a source "Sheet!Cell"
    IN THE SAME SHEET whose number format it should borrow instead of keeping its own.
    The universal template reuses some input cells for a different kind of number
    depending on industry (e.g. Assumptions!C25 is a ₹/unit raw-material cost for a
    factory but a 0-1 gross-margin fraction for a hotel) — the cell's own baked-in
    format is only ever right for one of those, so the caller borrows a format from a
    cell that already displays the OTHER kind correctly (e.g. Assumptions!C18, already
    formatted as "0.0%").

    Raises FileNotFoundError if the template is missing.
    """
    path = template_path(purpose_key, template_id)
    if not path:
        raise FileNotFoundError(f"Template not found: {purpose_key}/{template_id}")
    logger.info("fill: template selected %s/%s -> %s", purpose_key, template_id, path)

    # cell key -> field type, so we coerce values (percent/number/text) correctly.
    schema = load_schema(purpose_key, template_id)
    types = {}
    if schema:
        for g in schema.get("groups", []):
            for f in g.get("fields", []):
                types[field_key(g["sheet"], f["cell"])] = f.get("type", "number")

    style_overrides = style_overrides or {}

    with zipfile.ZipFile(path, "r") as zin:
        name_to_path = _sheet_name_to_path(zin)
        name_to_path_rev = {v: k for k, v in name_to_path.items()}

        # Group the target cells by the sheet XML part they live in.
        edits = {}  # sheet_xml_path -> list of (cell_ref, kind, value)
        for key, raw in (answers or {}).items():
            if "!" not in key:
                continue
            sheet, cell = key.rsplit("!", 1)
            spath = name_to_path.get(sheet)
            if not spath:
                continue
            coerced = _coerce(raw, types.get(key, "number"))
            if not coerced:
                continue
            edits.setdefault(spath, []).append((cell, coerced[0], coerced[1]))

        # Apply edits to each affected worksheet part; flip recalc in workbook.xml.
        modified = {}
        for spath, cells in edits.items():
            xml = zin.read(spath).decode("utf-8")
            for cell_ref, kind, value in cells:
                style_index = None
                override_key = f"{name_to_path_rev.get(spath)}!{cell_ref}" if style_overrides else None
                source_key = style_overrides.get(override_key) if override_key else None
                if source_key and "!" in source_key:
                    src_sheet, src_cell = source_key.rsplit("!", 1)
                    src_spath = name_to_path.get(src_sheet)
                    if src_spath == spath:  # same-sheet borrow only — see docstring
                        style_index = _cell_style_index(xml, src_cell)
                xml, _ = _set_cell_xml(xml, cell_ref, kind, value, style_index=style_index)
            modified[spath] = xml.encode("utf-8")

        wb_path = "xl/workbook.xml"
        wb_xml = zin.read(wb_path).decode("utf-8")
        modified[wb_path] = _set_full_calc_on_load(wb_xml).encode("utf-8")
        cells_written = sum(len(v) for v in edits.values())
        logger.info("fill: modified %d worksheet part(s), %d cell(s) + workbook.xml",
                    len(edits), cells_written)

        # Repackage: copy every entry verbatim except the ones we changed. This
        # keeps drawings, chart styles, VML, VBA, media, etc. exactly intact.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = modified.get(item.filename)
                if data is None:
                    data = zin.read(item.filename)
                # Preserve the original entry metadata (name, date, external attrs).
                zi = zipfile.ZipInfo(item.filename, date_time=item.date_time)
                zi.compress_type = item.compress_type
                zi.external_attr = item.external_attr
                zi.internal_attr = item.internal_attr
                zi.create_system = item.create_system
                zout.writestr(zi, data)

    out = buf.getvalue()
    logger.info("fill: workbook saved (%d bytes); validating…", len(out))
    _validate_workbook(out, list(modified.keys()))
    logger.info("fill: workbook validated OK")
    return out


def template_filename(purpose_key: str, template_id: str, base: str) -> str:
    """Download filename preserving the template's extension (.xlsx/.xlsm)."""
    path = template_path(purpose_key, template_id) or ""
    ext = ".xlsm" if path.endswith(".xlsm") else ".xlsx"
    return f"{base}{ext}"

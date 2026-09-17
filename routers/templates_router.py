"""
templates_router.py

Exposes the sample templates behind each purpose and their input schemas so the
frontend can (a) let the user pick which sample layout to generate against and
(b) render that template's own questions (pre-filled with the sample's values).

Public, like the industry/country/purpose lookups — these return no user data.
"""

import logging
import shutil

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from purpose_config import resolve_purpose, get_config, PURPOSES
from template_config import (
    list_templates, find_template_by_id, is_dynamic_template,
    remove_dynamic_template, UPLOAD_DIR,
)
from services.template_introspect import load_schema
from services.template_fill_service import field_key
from services.template_upload_service import (
    register_uploaded_template, scan_templates_dir, ALLOWED_EXT,
)
from dependencies import get_current_user
from models.user_model import User

logger = logging.getLogger("templates")

router = APIRouter(prefix="/templates", tags=["Templates"])


@router.get("/{purpose}")
def get_templates(purpose: str):
    """List the templates offered for a purpose. `purpose` may be a canonical
    purpose key OR the app's free-form purpose slug (e.g. 'venture_capital') —
    we fold it onto the modelling key the same way generation does."""
    purpose_key = resolve_purpose(purpose)
    out = []
    for t in list_templates(purpose_key):
        out.append({
            "id": t["id"],
            "label": t["label"],
            "currency": t.get("currency", ""),
            "engine": t["engine"],
            "available": t["engine"] == "template_fill",
        })
    return {"purpose_key": purpose_key, "purpose_label": get_config(purpose_key)["label"], "templates": out}


@router.get("/{purpose}/{template_id}/schema")
def get_template_schema(purpose: str, template_id: str):
    """The template's input schema: the questions to ask, grouped by sheet, each
    field carrying a stable `key` ("Sheet!Cell") and the sample's `default`."""
    purpose_key, t = find_template_by_id(template_id)
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")

    schema = load_schema(purpose_key, template_id)
    if not schema:
        raise HTTPException(
            status_code=404,
            detail="Input schema not generated for this template yet.",
        )

    # Attach a stable key to every field so answers round-trip to the right cell.
    for g in schema.get("groups", []):
        for f in g.get("fields", []):
            f["key"] = field_key(g["sheet"], f["cell"])
    schema["label"] = t["label"]
    schema["template_id"] = template_id
    schema["purpose_key"] = purpose_key
    return schema


@router.post("/upload")
async def upload_template(
    file: UploadFile = File(...),
    label: str = Form(""),
    purpose: str = Form(""),
    currency: str = Form("INR"),
    universal: bool = Form(None),
    current_user: User = Depends(get_current_user),
):
    """Upload an Excel (.xlsx/.xlsm) or Word (.docx) template. The server detects its
    structure, generates the input schema, and registers it — no code changes, and it
    becomes available in the template list immediately. `purpose` optional (blank ->
    offered under every purpose). Formulas/charts/tables/layout are preserved intact."""
    content = await file.read()
    purpose_key = resolve_purpose(purpose) if purpose else ""
    try:
        meta = register_uploaded_template(
            content, file.filename, label=label, purpose=purpose_key,
            currency=(currency or "INR"), universal=universal,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("template upload failed")
        raise HTTPException(status_code=409, detail=f"Could not register template: {e}")
    logger.info("template uploaded by user=%s -> %s (%s, %d fields)",
                current_user.id, meta["template_id"], meta["engine"], meta["fields"])
    if meta["fields"] == 0:
        meta["warning"] = (
            "No input fields were detected. For Excel, mark input cells with a blue "
            "font; for Word, use {{placeholder}} tokens. The template is registered but "
            "will generate with no editable inputs until it has some."
        )
    return meta


@router.post("/scan")
def scan_templates(current_user: User = Depends(get_current_user)):
    """Register any template files placed under templates/<category>/ that aren't
    registered yet — for when templates are added by dropping files into folders
    rather than uploading. Idempotent; returns the newly-registered templates."""
    added = scan_templates_dir()
    logger.info("template scan by user=%s -> %d new", current_user.id, len(added))
    return {"registered": added, "count": len([a for a in added if "template_id" in a])}


@router.delete("/{template_id}")
def delete_template(template_id: str, current_user: User = Depends(get_current_user)):
    """Remove an uploaded template (registry entry + its files). Built-in templates
    cannot be deleted this way."""
    if not is_dynamic_template(template_id):
        raise HTTPException(status_code=404, detail="No uploaded template with that id.")
    remove_dynamic_template(template_id)
    import os
    dir_path = os.path.join(UPLOAD_DIR, template_id)
    if os.path.isdir(dir_path):
        shutil.rmtree(dir_path, ignore_errors=True)
    logger.info("template deleted by user=%s -> %s", current_user.id, template_id)
    return {"deleted": template_id}


@router.get("")
def all_templates():
    """Flat list of every currently-available template (built-in + uploaded), across
    all purposes — handy for an admin/manage view."""
    seen, out = set(), []
    for purpose_key in PURPOSES:
        for t in list_templates(purpose_key):
            if t["id"] in seen:
                continue
            seen.add(t["id"])
            out.append({
                "id": t["id"], "label": t["label"], "engine": t["engine"],
                "currency": t.get("currency", ""), "purpose": purpose_key,
                "universal": bool(t.get("universal")),
                "uploaded": bool(t.get("uploaded")),
                "available": t["engine"] in ("template_fill", "docx_fill"),
            })
    return {"templates": out, "allowed_upload_types": list(ALLOWED_EXT)}

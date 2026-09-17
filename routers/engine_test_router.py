"""
engine_test_router.py

TEMPORARY validation surface for the new Python financial engine.

  GET  /engine-test/generate  -> .xlsx built from the built-in demo assumptions
  POST /engine-test/generate  -> .xlsx built from caller-supplied assumptions
  GET  /engine-test/validate  -> JSON only (validation + metadata, no file)

It exists ONLY to prove the engine runs end to end and produces a populated workbook.
It touches nothing in production:

  * generation_router, its /generate/{id}/excel endpoint and the formula-driven
    template-fill workflow are untouched;
  * the bank-loan pipeline is untouched;
  * it writes into a SEPARATE value-sink workbook (templates/engine_test/), never a
    production template, so no formulas are ever overwritten.

It reuses the existing pipeline wholesale via services.excel_builder.build_excel_workbook
— no pipeline logic is duplicated here.

NOTE: this route is unauthenticated by design (it reads no project and no user data —
it only computes from assumptions it is handed). Remove or secure it before this ever
ships to production.
"""

import logging
import os
import tempfile
from io import BytesIO

from fastapi import Depends, APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from dependencies import get_admin_user
from pydantic import BaseModel, Field

from engine_test.template_definition import (
    DEMO_ASSUMPTIONS, TEMPLATE_DEFINITION, ensure_template,
)
from services.excel_builder import build_excel_workbook

logger = logging.getLogger("engine_test")

# STAFF ONLY, and see main.py — this router is not mounted at all when ENV is
# production. Two layers on purpose: the mount decides whether the harness exists on
# a given deployment, the dependency decides who may use it where it does.
router = APIRouter(prefix="/engine-test", tags=["Engine Test (temporary)"], dependencies=[Depends(get_admin_user)])
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class EngineTestRequest(BaseModel):
    assumptions: dict = Field(default_factory=dict,
                              description="Engine-native assumptions dict; empty = use the demo set")


def _run(assumptions: dict) -> dict:
    """Run the canonical pipeline into a temp output file. Returns build_excel_workbook's
    result dict plus the bytes. Reuses the existing orchestrator verbatim."""
    template_path = ensure_template()
    out_dir = tempfile.mkdtemp(prefix="engine_test_")
    output_path = os.path.join(out_dir, "engine_test_model.xlsx")

    result = build_excel_workbook(
        assumptions=assumptions,
        template_path=template_path,
        output_path=output_path,
        template_definition=TEMPLATE_DEFINITION,
    )
    with open(output_path, "rb") as fh:
        result["_bytes"] = fh.read()
    return result


def _stream(result: dict) -> StreamingResponse:
    data = result.pop("_bytes")
    v = result["validation"]
    logger.info("engine-test: workbook %d bytes, validation passed=%s errors=%d",
                len(data), v["passed"], len(v["errors"]))
    return StreamingResponse(
        BytesIO(data), media_type=XLSX_MIME,
        headers={
            "Content-Disposition": 'attachment; filename="engine_test_model.xlsx"',
            # surface the engine's own verdict without changing the download contract
            "X-Engine-Validation-Passed": str(v["passed"]),
            "X-Engine-Validation-Errors": str(len(v["errors"])),
            "X-Engine-Version": str(result["metadata"].get("engine_version")),
        },
    )


@router.get("/generate")
def generate_demo():
    """Build a workbook from the built-in demo assumptions (browser-callable)."""
    try:
        return _stream(_run(DEMO_ASSUMPTIONS))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("engine-test: demo generation failed")
        raise HTTPException(status_code=409, detail=f"Engine test failed: {e}")


@router.post("/generate")
def generate(req: EngineTestRequest):
    """Build a workbook from caller-supplied engine assumptions (falls back to demo)."""
    try:
        return _stream(_run(req.assumptions or DEMO_ASSUMPTIONS))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("engine-test: generation failed")
        raise HTTPException(status_code=409, detail=f"Engine test failed: {e}")


@router.get("/validate")
def validate():
    """Run the pipeline and return only the engine's validation + metadata (no file)."""
    try:
        result = _run(DEMO_ASSUMPTIONS)
        result.pop("_bytes", None)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("engine-test: validate failed")
        raise HTTPException(status_code=409, detail=f"Engine test failed: {e}")

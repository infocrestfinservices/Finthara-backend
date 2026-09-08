"""
generation_router.py

Purpose-driven generation pipeline. The selected purpose decides the
methodology, report sections, workbook sheets and charts (see purpose_config).

Endpoints (all require auth + project ownership):
  POST /generate/{project_id}            -> run agents + build structured model, persist
  GET  /generate/{project_id}/excel      -> stream the .xlsx workbook
  GET  /generate/{project_id}/word       -> stream the .docx report
"""

import json
import logging
import re
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models.project_model import Project
from models.report_model import Report
from models.questionnaire_model import QuestionnaireAnswer
from dependencies import get_owned_project, get_current_user
from models.user_model import User
from services.entitlements import may_generate, may_export

from purpose_config import resolve_purpose, get_config
from template_config import (default_template, get_template, find_template_by_id,
                             template_path, _load_registry)
from services.financial_model_service import generate_financial_model
from services.excel_builder import build_excel
from services.excel_model_builder import build_model_excel
from services.word_builder import build_word
from services.sample_blueprint_service import build_blueprint_text, has_sample
from services.template_fill_service import fill_template, template_filename
from services import optional_sheets
from services.template_model_service import generate_template_inputs, derive_headline_kpis
from services.template_introspect import load_schema
from services.recalc_service import libreoffice_available, recalculate_xlsx, read_computed_kpis
from services.template_analysis import load_analysis, extract_kpis, run_checks
from agents.market_research_agent import market_research_agent
from agents.feasibility_agent import feasibility_agent
from agents.swot_agent import swot_agent

router = APIRouter(prefix="/generate", tags=["Generation"])
logger = logging.getLogger("generation")

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLSM_MIME = "application/vnd.ms-excel.sheet.macroEnabled.12"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_PROJECT_FIELDS = [
    "id", "title", "industry", "sub_industry", "country", "currency", "location",
    "promoter_name", "promoter_experience", "project_description", "target_market",
    "target_customers", "project_cost", "own_contribution", "loan_amount", "purpose",
    "financial_format", "created_at",
]


class GenerateRequest(BaseModel):
    purpose_answers: dict = {}
    # Which sample template the user picked (see template_config). Cell-keyed
    # answers ("Sheet!Cell": value) inside purpose_answers fill that template.
    template_id: str | None = None
    # Ask the AI for a brand-new set of input assumptions instead of reusing the ones
    # already on file. Off by default so a regeneration reproduces the same model.
    refresh_inputs: bool = False
    # The user's own words about what they want changed this time round — e.g. "show a
    # monthly revenue break-up" or "drop the market research section". Remembered with
    # the project so later regenerations keep honouring it.
    instructions: str | None = None
    # Build the WORKBOOK only and skip the written report. The narrative is the single
    # expensive call in the pipeline and the Excel does not use it, so this is what a
    # "just let me see the numbers" run should do. Default True because that is the
    # everyday case; pass excel_only=false when the Word/PDF report is actually wanted.
    excel_only: bool = True


# Reserved key used to remember the chosen template inside the persisted answers.
_TEMPLATE_KEY = "_template_id"
# Which template the stored "Sheet!Cell" answers were actually built for.
_FILLED_TEMPLATE_KEY = "_filled_template_id"
# The user's free-text requirements for the report.
_INSTRUCTIONS_KEY = "_user_instructions"


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", (text or "report")).strip("_")[:40] or "report"


def _stored_answers(db: Session, project: Project) -> dict:
    """Load persisted questionnaire answers for a project (or {})."""
    q = db.query(QuestionnaireAnswer).filter(QuestionnaireAnswer.project_id == project.id).first()
    if q and q.collected_data:
        try:
            data = json.loads(q.collected_data)
        except (ValueError, TypeError):
            return {}
        # Strip here too (not just on save) so a project saved under the old
        # extract_wc_seed() code is clean on THIS load — the very next fill_template
        # call, not just the one after — and never re-applies stale "WC & CC-OD
        # Limit" values to the sheet's live formula cells.
        for k in _STALE_WC_SEED_KEYS:
            data.pop(k, None)
        return data
    return {}


_STALE_WC_SEED_KEYS = (
    "WC & CC-OD Limit!C5", "WC & CC-OD Limit!C6", "WC & CC-OD Limit!C8",
    "WC & CC-OD Limit!C12", "WC & CC-OD Limit!C13",
)


def _persist_answers(db: Session, project: Project, answers: dict) -> None:
    """Upsert the project's questionnaire answers (incl. AI-generated cell values)."""
    # Self-heal projects saved before the extract_wc_seed() removal: those 5 keys hold
    # static numbers that were clobbering "WC & CC-OD Limit" formula cells on every
    # fill. Strip them here so any project generated under the old code recovers its
    # live Form_IV_CA_CL links on its very next generate/regenerate, no manual DB fix
    # needed.
    if answers:
        for k in _STALE_WC_SEED_KEYS:
            answers.pop(k, None)
    q = db.query(QuestionnaireAnswer).filter(QuestionnaireAnswer.project_id == project.id).first()
    payload = json.dumps(answers or {})
    if q:
        q.collected_data = payload
    else:
        db.add(QuestionnaireAnswer(project_id=project.id, collected_data=payload))
    db.commit()


def _industry_template(industry: str):
    """A dedicated industry template whose CALCULATIONS (not just labels) model that
    industry — e.g. retail's revenue = transactions × average bill and COGS = sales ×
    (1 − gross margin), instead of the manufacturing capacity/per-unit model. Matched
    by the template folder ('retail_cma' → retail). Returns (purpose, template) or
    (None, None). Manufacturing and any industry without a dedicated template fall
    through to the generic CMA template, unchanged."""
    if not industry:
        return None, None
    # Resolve the industry to its canonical operating-model slug (handles display
    # names like "Food & Beverage / Restaurant" -> "restaurant"), then find the
    # registered template whose folder is "<slug>_cma".
    try:
        from financial_engine.industry_calc.operating_models import get_operating_model
        m = get_operating_model(industry)
    except Exception:
        m = None
    slug = m.key if m else re.sub(r"[^a-z0-9]", "", industry.casefold())
    if not slug:
        return None, None
    target = f"{slug}_cma"
    try:
        for pk, entries in _load_registry().items():
            for t in entries:
                if (t.get("folder") or "") == target and t.get("engine") == "template_fill":
                    return pk, t
    except Exception:
        logger.warning("industry template lookup failed for %r", industry, exc_info=True)
    return None, None


# Purposes whose report is a bank-style CMA: these keep the INDUSTRY template (the
# CMA family already is the bank-loan format). Every other purpose gets its own.
_CMA_PURPOSES = {"bank_loan", "cma_data", "term_loan", "project_finance", "dpr"}


def _purpose_template(app_purpose: str):
    """A template dedicated to the REPORT PURPOSE — a government grant must produce a
    grant workbook, a VC round a VC workbook, not the bank-loan CMA. Matched by the
    template folder ('government_grant' → government_grant purpose). Returns
    (purpose, template) or (None, None).

    Bank-loan style purposes are deliberately excluded: for those the CMA family IS
    the right format, so the industry template wins instead."""
    def _slugify(v):
        return re.sub(r"[^a-z0-9_]", "", str(v or "").casefold().replace(" ", "_"))

    # Try the raw app purpose first, then its canonical key ("PMEGP" -> government_grant).
    candidates = [_slugify(app_purpose)]
    try:
        from purpose_config import resolve_purpose
        candidates.append(_slugify(resolve_purpose(app_purpose, None)))
    except Exception:
        pass
    candidates = [c for c in dict.fromkeys(candidates) if c and c not in _CMA_PURPOSES]
    if not candidates:
        return None, None
    try:
        registry = _load_registry().items()
        for slug in candidates:
            for pk, entries in registry:
                for t in entries:
                    if (t.get("folder") or "") == slug and t.get("engine") == "template_fill":
                        # only if the workbook is actually on disk
                        if template_path(pk, t["id"]):
                            return pk, t
    except Exception:
        logger.warning("purpose template lookup failed for %r", app_purpose, exc_info=True)
    return None, None


def _resolve_template(purpose_key: str, answers: dict, industry: str = None,
                      app_purpose: str = None):
    """The template to fill, in priority order:
      1. the user's explicitly chosen template (by globally-unique id);
      2. a template dedicated to the REPORT PURPOSE (grant / VC / angel …) — the
         purpose decides what KIND of report this is;
      3. a template dedicated to the project's INDUSTRY (real industry-specific
         calculations) — used for bank-loan/CMA purposes, where the industry decides
         the operational model;
      4. the purpose's default.
    Returns (purpose_key, template) or (purpose_key, None) if none applies."""
    tid = (answers or {}).get(_TEMPLATE_KEY)
    if tid:
        tpurpose, t = find_template_by_id(tid)
        if t and t.get("engine") == "template_fill":
            return tpurpose, t
    # INDUSTRY first, then purpose. The industry workbooks are the ones we generate and
    # have validated end-to-end (revenue = the industry's own driver, margin-based cost
    # of sales, sane ratios). The third-party purpose workbooks (grant / VC / angel) are
    # NOT calibrated to our input schema: filling them produced revenue of ~0 against
    # billions of cost, i.e. a meaningless model — verified on the grant workbook. A
    # business's financial model does not change with the reason for the report; the
    # ANALYSIS does, and that is carried by the purpose's Word sections. So the numbers
    # come from the industry model and the purpose frames them.
    ipurpose, itmpl = _industry_template(industry)
    if itmpl:
        return ipurpose, itmpl
    ppurpose, ptmpl = _purpose_template(app_purpose)
    if ptmpl:
        return ppurpose, ptmpl
    return purpose_key, default_template(purpose_key)


def _project_dict(project: Project, answers: dict) -> dict:
    d = {f: getattr(project, f, None) for f in _PROJECT_FIELDS}
    # purpose_answers is json.dumps'd into BOTH the narrative and the input-fill prompts as
    # the model's "numeric inputs" block. Internal bookkeeping keys — above all the ~35 KB
    # cached _agent_context — are NOT model inputs. Dumping them ballooned the narrative
    # prompt to ~53 K chars; on the v4 reasoning model that overflow made it burn the entire
    # 32 K output budget on reasoning and return EMPTY content ("Empty model response" -> 502),
    # and it doubled the token cost of every call. Keep only the real answers (cell values +
    # questionnaire); every "_"-prefixed key is stripped. agent_context still reaches the
    # model through its own dedicated (truncated) argument.
    d["purpose_answers"] = {k: v for k, v in (answers or {}).items()
                            if not str(k).startswith("_")}
    return d


# The guards address Assumptions cells by POSITION (C8 term loan, C16 volume, C21 monthly
# phasing, C58:D62 segments, C66:D69 streams, ...). Those positions are a property of the
# CMA workbook family, not of spreadsheets in general: on the angel-investment model C58 is
# "Promoter's / Founder Equity" and on the MSME grant model C21 is "Margin money for
# working capital". Running the chain there would write a segment name into an equity cell.
# So the chain is scoped to the family whose layout it was written for.
_CMA_LAYOUT_IDS = {"bank_loan_cma"}
_CMA_LAYOUT_PREFIX = "cma_"


def _has_cma_layout(template) -> bool:
    tid = (template or {}).get("id") or ""
    return tid in _CMA_LAYOUT_IDS or tid.startswith(_CMA_LAYOUT_PREFIX)


def _is_capacity_layout(template) -> bool:
    """bank_loan_cma is the MANUFACTURING workbook: C25 is a per-unit raw-material cost,
    there is no streams block, and the driver sheets are Production/Sales."""
    return ((template or {}).get("id") or "") in _CMA_LAYOUT_IDS


def _reconcile_on_read(template) -> bool:
    """Should a READ (download / review) re-run the guard chain before rendering?

    Only for the industry workbooks. Their stored answers predate the volume x price
    build-up, so without the chain a download renders every revenue stream as zero.

    The manufacturing workbook is included now that it, too, carries ancillary output
    streams: without the chain an older bank-loan project would render all four as zero,
    exactly as the industry ones did. What keeps that safe is _AsCapacity — a retail or
    service project sitting on this workbook still has the volume-price guards stood down,
    so its per-unit cost cells are never reinterpreted as margins.
    """
    return _has_cma_layout(template)


class _AsCapacity:
    """Presents a project to the guards as a capacity-family business.

    The family-gated guards (gross-margin clamp, streams, labour band, capex profile,
    monthly phasing) decide from the project's INDUSTRY. That is right when the industry
    template is in play, but a retail or service project can be sitting on the
    manufacturing bank_loan workbook — and there C25 means rupees-per-unit, not a margin
    fraction. Clamping it to 0-1 collapsed a real report's inventory purchase from
    Rs 14.76 crore to Rs 2.16 lakh. The workbook's layout, not the client's trade, decides
    which guards may run.
    """

    industry = "Manufacturing"

    def __init__(self, project):
        self._project = project

    def __getattr__(self, name):
        return getattr(self._project, name)


# How many times the scale/streams/labour trio may be re-run before we stop and say so.
_SCALE_PASSES = 3


def _num_or_none(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _reconcile_all(answers: dict, project: Project, template=None) -> dict:
    """The deterministic post-AI guard chain, in the one order that is correct.

    Every step is pure arithmetic over the answers — no LLM call, so this is free and
    reproducible, and it is safe to run on a plain download as well as a generation.
    Both paths MUST run it: the workbook is filled straight from these cells, so a
    download that skipped the chain shipped a different (and sometimes broken) model
    from the one the user generated. That is exactly what happened to projects created
    before the streams became a volume x price build-up — their C66:C69 still held rupee
    AMOUNTS, and without this chain the download rendered every stream as zero.

    Pass the resolved template so the chain can stand down on a workbook whose Assumptions
    sheet is laid out differently; with no template given it runs, preserving the original
    behaviour for callers that have already established the family.
    """
    if template is not None and not _has_cma_layout(template):
        logger.info("reconcile: template %r is not the CMA layout; guards stood down",
                    (template or {}).get("id"))
        return answers
    if template is not None and _is_capacity_layout(template):
        # The manufacturing workbook — the volume_price guards must not touch it, whatever
        # trade the client is in.
        if getattr(project, "industry", None):
            from financial_engine.industry_calc.operating_models import family_of
            if family_of(project.industry) == "volume_price":
                logger.info("reconcile: %r project on the manufacturing workbook; "
                            "volume-price guards stood down", project.industry)
                project = _AsCapacity(project)
    from services.financing_reconciler import (reconcile_financing, reconcile_segments,
                                               reconcile_scale, reconcile_industry,
                                               reconcile_identity, reconcile_phasing,
                                               reconcile_capex, reconcile_working_capital,
                                               reconcile_streams, reconcile_operating_costs,
                                               reconcile_drivers, reconcile_existing_loan,
                                               relabel_streams)
    # First: without numeric year/month drivers every projected cell evaluates to
    # #VALUE!, and nothing downstream can be judged.
    answers = reconcile_drivers(answers, project)
    answers = reconcile_existing_loan(answers, project)
    answers = reconcile_identity(answers, project)
    answers = reconcile_industry(answers, project)
    answers = reconcile_financing(answers, project)
    answers = reconcile_capex(answers, project)
    answers = reconcile_working_capital(answers, project)
    # Scale, streams and labour are MUTUALLY dependent, and running them once in a line is
    # not enough. The volume is solved from the fixed costs; the streams are sized off the
    # volume; the wage bill is pegged to the revenue the streams complete — and that wage
    # bill is part of the fixed cost the volume came from. On the solar plant (#59) the
    # first pass raised the capacity against a ~Rs 22.4 L monthly cost base, then the wage
    # guard cut that base to Rs 5.6 L, and nothing went back to the volume: the plant was
    # left generating exactly 4x what it needed to, a 5.76 MW output on a 2.1 MW capex.
    # Iterating to a fixed point removes the whole class of error rather than that one
    # instance. Two passes settle every project measured; the third is a stop, not a plan.
    for _pass in range(_SCALE_PASSES):
        before = _num_or_none(answers.get("Assumptions!C16"))
        # Streams run AFTER scale: it sizes the volume/price cells the stream seed derives
        # from, so seeding first would peg the ancillary income to a capacity then discarded.
        answers = reconcile_scale(answers, project)
        answers = reconcile_streams(answers, project)
        # Labour is measured against TOTAL revenue, so it must run AFTER the streams are
        # sized — that is what makes the ancillary income carry its share of the staffing.
        answers = reconcile_operating_costs(answers, project)
        after = _num_or_none(answers.get("Assumptions!C16"))
        if before is None or after is None or abs(after - before) <= abs(before) * 0.005:
            break
    else:
        logger.warning("reconcile: volume still moving after %d passes (now %s) — the "
                       "guards are fighting each other on this project", _SCALE_PASSES,
                       answers.get("Assumptions!C16"))
    # Text only, and safe anywhere after the streams exist: it renames those four rows in
    # the industry's own vocabulary when the industry is using a borrowed workbook.
    answers = relabel_streams(answers, project)
    answers = reconcile_segments(answers, project)
    answers = reconcile_phasing(answers, project)
    return answers


def _enriched_description(project: Project, purpose_key: str, answers: dict) -> str:
    """Feed purpose + questionnaire answers into the existing agents (no new
    agent, no signature change) by enriching the description text."""
    cfg = get_config(purpose_key)
    lines = [project.project_description or ""]
    lines.append(f"\nREPORT PURPOSE: {cfg['label']}")
    if answers:
        lines.append("KEY FINANCIAL INPUTS: " + json.dumps(answers))
    return "\n".join(lines)


_AGENT_CACHE_KEY = "_agent_context"


def _build_agent_context(project: Project, purpose_key: str, answers: dict,
                         refresh: bool = False) -> str:
    """Reuse the existing agents to produce supporting analysis, now purpose-aware.

    The three agents are independent, so we run them concurrently (each call is
    blocking network I/O) to cut total latency from sum-of-three to ~one call.

    They describe the BUSINESS (market, feasibility, SWOT), which does not change between
    runs — so the combined output is cached in the answers blob and REUSED on a plain
    regeneration instead of re-calling three LLM agents every time (that was ~60% of a
    regeneration's API cost for no new information). A refresh (or the very first run,
    when there is no cache) rebuilds it.
    """
    from concurrent.futures import ThreadPoolExecutor

    cached = answers.get(_AGENT_CACHE_KEY) if isinstance(answers, dict) else None
    if cached and not refresh:
        logger.info("generate: project=%s reusing cached business analysis (0 agent calls)",
                    getattr(project, "id", "?"))
        return cached

    name = project.title or "the business"
    industry = project.industry or "General"
    country = project.country or "India"
    desc = _enriched_description(project, purpose_key, answers)
    pc = project.project_cost or 0
    oc = project.own_contribution or 0
    loan = project.loan_amount or 0
    label = get_config(purpose_key)["label"]

    def _safe(title, fn):
        try:
            return f"{title}:\n" + fn()
        except Exception as e:  # agent failures must not abort generation
            return f"{title}: (unavailable: {e})"

    tasks = {
        # location + target_market are what make the research REGIONAL: without them the
        # competitor set came back national and a banker in Indore could not use it.
        "MARKET RESEARCH": lambda: market_research_agent(
            business_name=name, industry=industry, country=country, purpose=label,
            description=desc, location=(getattr(project, "location", "") or ""),
            target_market=(getattr(project, "target_market", "") or "")),
        "FEASIBILITY": lambda: feasibility_agent(
            business_name=name, industry=industry, country=country,
            description=desc, project_cost=pc, own_contribution=oc, loan_amount=loan),
        "SWOT": lambda: swot_agent(
            business_name=name, industry=industry, country=country, description=desc),
    }

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {title: pool.submit(_safe, title, fn) for title, fn in tasks.items()}
        # Preserve a stable order in the combined context.
        parts = [futures[title].result() for title in ("MARKET RESEARCH", "FEASIBILITY", "SWOT")]
    ctx = "\n\n".join(parts)
    # Cache it so the next regeneration reuses it (persisted with the other answers). Only
    # store a genuinely-built context, never a run where all three agents failed.
    if isinstance(answers, dict) and "(unavailable" not in ctx[:60]:
        answers[_AGENT_CACHE_KEY] = ctx
    return ctx


def _preview_markdown(model: dict, purpose_key: str, project: Project,
                      excel_only: bool = False) -> str:
    """The short on-screen summary — NOT the report.

    This used to be the whole document: every narrative section, then every financial
    statement as a markdown table. It was the only thing that read the model's "sheets"
    array, and printing it billed a second rendering of figures the workbook already holds
    authoritatively. The screen now says what the project is and what the headline numbers
    are; the two deliverables are downloaded from the buttons beneath it.
    """
    cfg = get_config(purpose_key)
    md = [f"# {project.title or 'Project Report'}", f"_{cfg['label']}_\n"]

    about = str(getattr(project, "project_description", "") or "").strip()
    where = str(getattr(project, "location", "") or "").strip()
    activity = str(getattr(project, "sub_industry", "") or
                   getattr(project, "industry", "") or "").strip()
    lead = " · ".join(x for x in (activity, where) if x)
    if lead:
        md.append(f"**{lead}**\n")
    if about:
        md.append(about + "\n")

    # The summary is the opening of what the model already wrote — never a second call.
    summary = str((model.get("narrative") or {}).get("Executive Summary") or "").strip()
    if summary:
        opening = " ".join(summary.split("\n")[0].split())
        if len(opening) > 460:
            cut = opening[:460]
            opening = cut[:cut.rfind(". ") + 1] if ". " in cut else cut.rsplit(" ", 1)[0] + "…"
        md.append("## Summary\n")
        md.append(opening + "\n")
    else:
        # No narrative — but that is only a SHORTFALL for a long report. The short report
        # does not read the narrative at all (see short_report.py), so telling its reader
        # "the written report was not generated" announced a failure that had not happened.
        # In its place: who the borrower is, built from their own record, costing nothing.
        md.append(_about_block(project))
        if not _is_short(project) and excel_only:
            md.append("\n_The written commentary was not generated for this run. To add it, "
                      "regenerate with the report option on — the stored inputs are reused, "
                      "so the numbers stay identical and only the prose is added._\n")

    cards = (model.get("financial_summary") or {}).get("cards") or {}
    rows = []
    for key, label in (("revenue_y1", "Revenue · Year 1"), ("revenue_y5", "Revenue · Year 5"),
                       ("ebitda_y5", "EBITDA · Year 5"), ("pat_y5", "Profit after tax · Year 5")):
        v = cards.get(key)
        if isinstance(v, (int, float)):
            rows.append((label, _money(v)))
    if isinstance(cards.get("avg_dscr"), (int, float)):
        rows.append(("Average loan cover (DSCR)", f"{cards['avg_dscr']:.2f}x"))
    if isinstance(cards.get("net_margin_y5"), (int, float)):
        rows.append(("Net margin · Year 5", f"{cards['net_margin_y5'] * 100:.1f}%"))
    if not rows:
        rows = [(k.get("label", ""), k.get("value", "")) for k in (model.get("kpis") or [])[:6]]
    if rows:
        md.append("## At a glance\n")
        md.append("| | |\n|---|---|")
        md += [f"| {label} | {value} |" for label, value in rows]
        md.append("")
    return "\n".join(md)


def _money(v):
    a = abs(v)
    if a >= 1e7:
        return f"₹{v / 1e7:,.2f} Cr"
    if a >= 1e5:
        return f"₹{v / 1e5:,.2f} L"
    return f"₹{v:,.0f}"


def _about_block(project: Project) -> str:
    """Who the borrower is — every line from the client's own record, so no model is asked
    and nothing here can be invented."""
    facts = [
        ("Promoter", getattr(project, "promoter_name", None)),
        ("Experience", getattr(project, "promoter_experience", None)),
        ("Line of activity", getattr(project, "sub_industry", None)
                             or getattr(project, "industry", None)),
        ("Location", getattr(project, "location", None) or getattr(project, "country", None)),
        ("Target market", getattr(project, "target_market", None)),
        ("Customers served", getattr(project, "target_customers", None)),
    ]
    facts = [(k, str(v).strip()) for k, v in facts if v and str(v).strip()]
    if not facts:
        return ""
    out = ["## About the company\n", "| | |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in facts]
    out.append("")
    return "\n".join(out)


class BrandingRequest(BaseModel):
    # data: URL from the browser's file reader, or "" to remove it
    logo_url: str | None = None
    brand_color: str | None = None


# Branding lives in the project's answers blob alongside the other reserved keys, so it
# needs no schema change on the live database — the same trick as _template_id.
_LOGO_KEY = "_logo_url"
_BRAND_COLOR_KEY = "_brand_color"


@router.get("/{project_id}/branding")
def get_branding(project: Project = Depends(get_owned_project),
                 db: Session = Depends(get_db)):
    """The saved logo and brand colour, so they survive a page reload."""
    answers = _stored_answers(db, project)
    return {"logo_url": answers.get(_LOGO_KEY, ""),
            "brand_color": answers.get(_BRAND_COLOR_KEY, "")}


@router.post("/{project_id}/branding")
def save_branding(req: BrandingRequest, project: Project = Depends(get_owned_project),
                  db: Session = Depends(get_db)):
    """Persist the report's logo and brand colour.

    The logo was previously held only in React state — it vanished on refresh and never
    reached the server, so it could not appear in the Word document."""
    answers = dict(_stored_answers(db, project))
    if req.logo_url is not None:
        if req.logo_url.strip():
            answers[_LOGO_KEY] = req.logo_url.strip()
        else:
            answers.pop(_LOGO_KEY, None)
    if req.brand_color is not None:
        if req.brand_color.strip():
            answers[_BRAND_COLOR_KEY] = req.brand_color.strip()
        else:
            answers.pop(_BRAND_COLOR_KEY, None)
    _persist_answers(db, project, answers)
    return {"logo_url": answers.get(_LOGO_KEY, ""), "brand_color": answers.get(_BRAND_COLOR_KEY, "")}


def _branding(db: Session, project: Project) -> dict:
    """{logo: bytes|None, brand_color: str|None} for the document builders."""
    answers = _stored_answers(db, project)
    out = {"logo": None, "brand_color": answers.get(_BRAND_COLOR_KEY) or None}
    data_url = answers.get(_LOGO_KEY)
    if isinstance(data_url, str) and data_url.startswith("data:"):
        try:
            import base64
            head, _, b64 = data_url.partition(",")
            if "base64" in head and b64:
                out["logo"] = base64.b64decode(b64)
        except Exception:
            logger.warning("branding: could not decode the logo for project %s", project.id,
                           exc_info=True)
    return out


# The client's own images, placed at the end of a named section. Stored beside the
# branding for the same reason — no schema change on the live database.
_INSERTS_KEY = "_section_inserts"
_MAX_INSERTS = 30
_MAX_INSERT_BYTES = 6 * 1024 * 1024      # per image, before base64 expansion


class SectionInsert(BaseModel):
    section: str
    data_url: str = ""
    caption: str = ""


class InsertsRequest(BaseModel):
    """The WHOLE list, as the client wants it — the same shape as the branding save, so
    adding and removing are one code path and cannot get out of step."""
    inserts: list[SectionInsert] = []


def _decode_data_url(data_url: str):
    """Bytes from a `data:` URL, or None. Never raises — a bad image must not stop a report."""
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None
    try:
        import base64
        head, _, b64 = data_url.partition(",")
        if "base64" not in head or not b64:
            return None
        raw = base64.b64decode(b64)
        return raw if raw else None
    except Exception:
        return None


@router.get("/{project_id}/cover")
def download_cover(project: Project = Depends(get_owned_project)):
    """The project's cover artwork, for the screen shown after generating.

    The SAME image the Word report's cover carries: it is generated once per project and
    cached on disk, so showing it here costs nothing and the screen cannot show a different
    picture from the document. Falls back to the bundled industry photograph, then to a
    drawn motif — so there is always something, and never a broken image.
    """
    from services.cover_art import cover_art
    try:
        data = cover_art(project.industry, _project_dict(project, {}))
    except Exception:
        logger.warning("cover: artwork unavailable for project %s", project.id, exc_info=True)
        data = None
    if not data:
        raise HTTPException(status_code=404, detail="No cover image for this project.")
    return StreamingResponse(BytesIO(data), media_type="image/jpeg",
                             headers={"Cache-Control": "public, max-age=86400"})


@router.get("/{project_id}/sections")
def list_sections(project: Project = Depends(get_owned_project),
                  db: Session = Depends(get_db)):
    """The report's main sections, so the UI can offer exactly the ones that exist.

    Read off a real composition rather than a hard-coded list: the sections a report
    raises depend on its purpose and on what was actually generated, and a stale menu
    would offer the client a section their report does not have.
    """
    from services.word_builder import report_sections
    try:
        model = _load_model(project)
    except HTTPException:
        return {"sections": []}
    answers = _stored_answers(db, project)
    try:
        titles = report_sections(model, resolve_purpose(project.purpose,
                                                        project.financial_format),
                                 _project_dict(project, answers))
    except Exception:
        logger.warning("sections: could not compose for project %s", project.id, exc_info=True)
        return {"sections": []}
    return {"sections": titles}


@router.get("/{project_id}/inserts")
def get_inserts(project: Project = Depends(get_owned_project),
                db: Session = Depends(get_db)):
    """What the client has inserted, so the panel survives a reload."""
    stored = _stored_answers(db, project).get(_INSERTS_KEY)
    return {"inserts": stored if isinstance(stored, list) else []}


@router.post("/{project_id}/inserts")
def save_inserts(req: InsertsRequest, project: Project = Depends(get_owned_project),
                 db: Session = Depends(get_db)):
    """Persist the client's inserted images."""
    if len(req.inserts) > _MAX_INSERTS:
        raise HTTPException(status_code=400,
                            detail=f"At most {_MAX_INSERTS} inserts per report.")
    clean = []
    for it in req.inserts:
        section = (it.section or "").strip()
        if not section:
            continue
        raw = _decode_data_url(it.data_url)
        if not raw:
            raise HTTPException(status_code=400,
                                detail=f"Could not read the image for “{section}”.")
        if len(raw) > _MAX_INSERT_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"The image for “{section}” is {len(raw) // (1024*1024)} MB — "
                       f"the limit is {_MAX_INSERT_BYTES // (1024*1024)} MB.")
        clean.append({"section": section, "data_url": it.data_url,
                      "caption": (it.caption or "").strip()})
    answers = dict(_stored_answers(db, project))
    if clean:
        answers[_INSERTS_KEY] = clean
    else:
        answers.pop(_INSERTS_KEY, None)
    _persist_answers(db, project, answers)
    return {"inserts": clean}


def _section_inserts(answers: dict) -> list:
    """[{section, image: bytes, caption}] for the document builder."""
    out = []
    for it in (answers.get(_INSERTS_KEY) or []):
        if not isinstance(it, dict):
            continue
        raw = _decode_data_url(it.get("data_url", ""))
        if raw and str(it.get("section") or "").strip():
            out.append({"section": str(it["section"]).strip(), "image": raw,
                        "caption": str(it.get("caption") or "").strip()})
    return out


@router.get("/{project_id}/answers")
def review_answers(project: Project = Depends(get_owned_project),
                   db: Session = Depends(get_db)):
    """The inputs this project's model was built from, labelled for review.

    Shown before a regeneration so the user can see exactly what was filled in and
    correct anything, instead of re-running blind and wondering why the numbers moved.

    Shows the RECONCILED inputs — the same deterministic chain the workbook is built
    from — so this screen and the delivered Excel can never disagree. Reading the raw
    stored cells instead meant an older project displayed its pre-migration figures (a
    stream volume of 70,000,000 at a price of 0) while the workbook correctly showed
    41,668 covers at ₹1,680, and the user had no way to tell which was real."""
    answers = _stored_answers(db, project)
    purpose_key = resolve_purpose(project.purpose, project.financial_format)
    tpurpose, template = _resolve_template(purpose_key, answers, project.industry,
                                           project.purpose)
    if template and _reconcile_on_read(template):
        answers = _reconcile_all(answers, project, template)
    labels, types, options = {}, {}, {}
    if template:
        schema = load_schema(tpurpose, template["id"]) or {}
        for g in schema.get("groups", []):
            for f in g.get("fields", []):
                key = f"{g['sheet']}!{f['cell']}"
                labels[key] = f.get("label") or f["cell"]
                types[key] = f.get("type", "number")
                if f.get("options"):
                    options[key] = f["options"]

    fields = []
    seen = set()
    for key, value in answers.items():
        if key.startswith("_") or "!" not in key:
            continue
        seen.add(key)
        fields.append({
            "key": key,
            "label": labels.get(key, key.split("!", 1)[1]),
            "value": value,
            "type": types.get(key, "number"),
            "options": options.get(key),
        })
    # Also show every input the SCHEMA defines that isn't stored yet — e.g. cells added to
    # the template after this project was first generated (like the hotel revenue streams).
    # Without this the user could never type a value into a newly-added input, so it stayed 0.
    for key in labels:
        if key not in seen:
            fields.append({
                "key": key,
                "label": labels[key],
                "value": "",
                "type": types.get(key, "number"),
                "options": options.get(key),
            })
    # Keep the workbook's own order so the list reads like the Assumptions sheet.
    order = {k: i for i, k in enumerate(labels)}
    fields.sort(key=lambda f: order.get(f["key"], 10_000))

    return {
        "project": {
            "title": project.title, "industry": project.industry,
            "currency": project.currency, "promoter_name": project.promoter_name,
            "project_cost": project.project_cost, "own_contribution": project.own_contribution,
            "loan_amount": project.loan_amount,
        },
        "template": {"id": template["id"], "label": template.get("label")} if template else None,
        # what the user last asked for in their own words, so the box comes back filled
        "instructions": answers.get(_INSTRUCTIONS_KEY, ""),
        "fields": fields,
    }


def _load_model(project: Project) -> dict:
    report = project.report
    if not report or not report.financial_model:
        raise HTTPException(status_code=404, detail="No financial model yet. Generate the report first.")
    try:
        return json.loads(report.financial_model)
    except (ValueError, TypeError):
        raise HTTPException(status_code=500, detail="Stored financial model is corrupted. Re-generate the report.")


# 402 Payment Required, not 403: the caller is who they say they are and owns the project —
# what is missing is a plan that covers this. The frontend keys the upgrade prompt off it,
# so it must not be conflated with an auth failure.
def _require(allowed_reason):
    allowed, why = allowed_reason
    if not allowed:
        raise HTTPException(status_code=402, detail=why)

@router.post("/{project_id}")
def generate(req: GenerateRequest, project: Project = Depends(get_owned_project),
             db: Session = Depends(get_db),
             current_user: User = Depends(get_current_user)):
    # A project that has already been generated passes: that is a REGENERATION of a report
    # the user has, not a new one, and the product actively encourages re-running it.
    _require(may_generate(db, current_user, project.id))
    purpose_key = resolve_purpose(project.purpose, project.financial_format)

    # Gather the answers: everything already stored for this project, with anything the
    # caller sent layered on top. It used to REPLACE the stored set whenever the request
    # carried any answer at all, so a regeneration that passed one field silently threw
    # away every previously computed input cell — the AI then refilled them from
    # scratch and the whole model (names, currency, every number) came back different.
    answers = dict(_stored_answers(db, project))
    answers.update(req.purpose_answers or {})
    if req.template_id:
        answers[_TEMPLATE_KEY] = req.template_id
    if req.instructions is not None:
        text = req.instructions.strip()
        if text:
            answers[_INSTRUCTIONS_KEY] = text
        else:
            answers.pop(_INSTRUCTIONS_KEY, None)

    # Reuse the cached market/feasibility/SWOT analysis on a plain regeneration; a refresh
    # (re-ask the AI) or the first run rebuilds it. Saves 3 of a regeneration's ~4 LLM calls.
    agent_context = _build_agent_context(project, purpose_key, answers,
                                         refresh=req.refresh_inputs)

    # What the user asked for in their own words. Deliberately NOT merged into
    # agent_context: that block also feeds the cell-filling AI (so a wording request
    # must never nudge a capacity or a price) and is parsed by section markers below.
    # It is handed to the narrative agent only, just before the call.
    user_ask = (answers.get(_INSTRUCTIONS_KEY) or "").strip()
    if user_ask:
        logger.info("generate: project=%s honouring user instructions (%d chars)",
                    project.id, len(user_ask))

    # AI generates the user's OWN model as values for the template's input cells.
    # The sample workbook is a DESIGN BLUEPRINT only — we never keep its numbers.
    # Any cell value the user supplied explicitly (form) overrides the AI value.
    tpurpose, template = _resolve_template(purpose_key, answers, project.industry, project.purpose)

    # Cell answers ("Sheet!Cell") are only meaningful for the template they were built
    # for: the same address means something completely different in another workbook.
    # If the resolved template has changed (e.g. the purpose now routes a grant to the
    # grant workbook instead of the CMA one), the old cell values would be written into
    # unrelated cells and produce garbage figures — so drop them and let the AI refill.
    if template:
        previous = answers.get(_FILLED_TEMPLATE_KEY)
        if previous and previous != template["id"]:
            stale = [k for k in answers if "!" in k]
            for k in stale:
                answers.pop(k, None)
            logger.info("generate: template changed %s -> %s; dropped %d stale cell answers",
                        previous, template["id"], len(stale))

    # Headline KPIs derived directly from the template's input cells (kept exact and
    # consistent with the editable Excel). Overrides the LLM's free-form KPIs for
    # template-fill purposes so the Word report never quotes a figure the Excel model
    # would contradict.
    derived_kpis = None
    consistency = None
    excel_summary = None
    market_segments = None
    statement_tables = None
    key_assumptions = None
    # Only AI-fill a template when its sample workbook still exists on disk. If the
    # samples were removed, skip the template track entirely and let the
    # deterministic formula-driven model (build_model_excel) be the output.
    if template and template_path(tpurpose, template["id"]):
        ai_inputs = {}
        existing_cells = [k for k in answers if "!" in k]
        # Schema input cells the stored answers don't cover yet — e.g. inputs ADDED to the
        # template AFTER this project was first generated (the per-industry revenue
        # streams). Without filling these, a regeneration reuses the old answers and the
        # new cells stay at the template's 0 default forever, so nothing ever changes.
        _sch = load_schema(tpurpose, template["id"]) or {}
        _schema_cells = {f"{g['sheet']}!{f['cell']}"
                         for g in _sch.get("groups", []) for f in g.get("fields", [])}
        missing_cells = [c for c in _schema_cells if c not in answers]
        # The AI only invents the input cells ONCE. Re-asking it on every regeneration is
        # what made a re-run produce a different business. With inputs already on file the
        # model is rebuilt from those exact figures, so ten regenerations give ten
        # identical workbooks. But NEW schema cells still have to be filled — the merge
        # below keeps every stored value and only the missing cells take the AI value, so
        # the existing figures stay identical while the new streams get populated.
        if existing_cells and not req.refresh_inputs and not missing_cells:
            logger.info("generate: project=%s reusing %d stored input cells (no AI refill)",
                        project.id, len(existing_cells))
        else:
            if missing_cells and existing_cells and not req.refresh_inputs:
                logger.info("generate: project=%s filling %d NEW schema cells (e.g. revenue "
                            "streams); the %d stored cells are kept unchanged",
                            project.id, len(missing_cells), len(existing_cells))
            try:
                ai_inputs = generate_template_inputs(
                    _project_dict(project, answers), tpurpose, template["id"], agent_context)
            except Exception as e:
                logger.exception("generate: AI template-input generation errored")
        user_cells = existing_cells
        # Never ship the untouched sample: if the AI produced no values (and the
        # user supplied none), fail loudly instead of returning the sample as-is.
        if not ai_inputs and not user_cells:
            raise HTTPException(
                status_code=502,
                detail="AI could not generate the financial-model inputs for this template. Please try again.",
            )
        answers = {**ai_inputs, **answers}  # user-provided "Sheet!Cell" values win
        answers[_FILLED_TEMPLATE_KEY] = template["id"]
        # Make the money tie to what the client actually said: the AI filled the
        # financing from their figures but costed the assets independently, so a ₹3cr
        # project financed by a ₹2.1cr loan showed only ₹40 lakh of assets — sources
        # did not equal uses, which is the first thing a CA/banker checks.
        from services.financing_reconciler import financing_check
        answers = _reconcile_all(answers, project, template)
        chk = financing_check(answers, project)
        logger.info("generate: project=%s financing ties=%s uses=%.0f sources=%.0f stated=%s",
                    project.id, chk["ties"], chk["uses_cost_of_project"],
                    chk["sources_loan_plus_equity"], chk["stated_project_cost"])
        logger.info("generate: project=%s AI-filled %d input cells for %s/%s",
                    project.id, len(ai_inputs), tpurpose, template["id"])
        tschema = load_schema(tpurpose, template["id"])
        if tschema:
            # Input-derived headline figures (exact, always available) as the baseline.
            derived_kpis = derive_headline_kpis(tschema, answers)
        # Template-driven analysis map (auto-detected KPI + consistency-check cells).
        analysis = load_analysis(tpurpose, template["id"])
        # If LibreOffice is present, recalculate the filled workbook ONCE on the server
        # and (a) read the EXACT computed metrics (DSCR/IRR/NPV/ROI/ratios) so every
        # output uses one set of numbers, and (b) run the template's consistency checks
        # (Sources=Uses / balance = 0). Non-fatal: on any failure we keep the input-
        # derived figures and generation still succeeds.
        legacy_kpis = bool(tschema and tschema.get("computed_kpis"))
        # Recalc once whenever LibreOffice is present: it yields both the template's
        # own KPIs/checks AND the shared 5-year financial summary that keeps the Word
        # report's numbers identical to the Excel model.
        if libreoffice_available():
            try:
                recalc = recalculate_xlsx(optional_sheets.apply(
                    fill_template(tpurpose, template["id"], answers), answers))
                kpis = extract_kpis(recalc, analysis)
                if not kpis and legacy_kpis:            # legacy hand-wired templates
                    kpis = read_computed_kpis(tschema, recalc)
                if kpis:
                    derived_kpis = kpis
                consistency = run_checks(recalc, analysis)
                from services.financial_summary_service import (extract_financial_summary,
                                                                extract_market_segments,
                                                                extract_statement_tables,
                                                                extract_key_assumptions)
                excel_summary = extract_financial_summary(recalc) or None
                market_segments = extract_market_segments(recalc) or None
                statement_tables = extract_statement_tables(recalc) or None
                key_assumptions = extract_key_assumptions(recalc) or None
                # NOTE: "WC & CC-OD Limit" C5/C6/C8/C12/C13 are LIVE FORMULAS in the
                # current template (=Form_IV_CA_CL!C14 etc.) — they must never be seeded
                # with static values here. A previous extract_wc_seed() call used to do
                # exactly that (leftover from before the sheet was formula-linked) and
                # was silently clobbering these formulas with wrongly-mapped numbers on
                # every generation. Removed — do not reintroduce without first checking
                # the sheet no longer has live Form_IV_CA_CL links at those cells.
                logger.info("generate: project=%s recalc KPIs=%d checks=%s summary=%s", project.id,
                            len(kpis), {c["name"][:22]: c["ok"] for c in (consistency or [])},
                            bool(excel_summary))
            except Exception:
                logger.warning("generate: server recalc/analysis failed; using input-derived KPIs",
                               exc_info=True)

    _persist_answers(db, project, answers)

    # Ground the narrative in the exact, input-derived figures so the prose can't
    # quote numbers that disagree with the Excel model; defer computed ratios to it.
    if derived_kpis:
        verified = "\n".join(f"- {k['label']}: {k['value']}" for k in derived_kpis)
        agent_context += (
            "\n\nVERIFIED HEADLINE FIGURES (use these EXACT numbers in the narrative; "
            "do NOT invent different ones. For DSCR, IRR, NPV and detailed year-by-year "
            "ratios, refer the reader to the accompanying Excel model rather than quoting "
            "a specific figure):\n" + verified)

    sample_blueprint = build_blueprint_text(purpose_key) or ""
    # The user's requirements are passed as their OWN prompt block, not folded into
    # agent_context: that block is presented to the model as "supporting analysis ... do
    # not contradict" and is truncated to 4000 chars, so a request buried in it lost to
    # the hard output rules and the report came back identical.
    if req.excel_only:
        # The written report is the ONE expensive call in this pipeline — minutes of
        # reasoning on the heavy model — and the Excel workbook does not use a word of it:
        # every figure there comes from the input cells and the sheet's own formulas. So
        # when the caller only wants the workbook, the call is skipped entirely and the
        # model is assembled from what the recalculated Excel already gives us.
        #
        # Nothing is lost permanently. The input cells are stored, and a later run with
        # excel_only=False reuses them, so the numbers come back IDENTICAL and only the
        # prose is added.
        model = {"narrative": {}, "kpis": [], "sheets": []}
        logger.info("generate: project=%s excel_only — narrative skipped, no heavy call",
                    project.id)
    else:
        try:
            model = generate_financial_model(_project_dict(project, answers), purpose_key,
                                             agent_context, sample_blueprint=sample_blueprint,
                                             user_instructions=user_ask)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Model generation failed: {e}")

    # Workbook annex — the SWOT and Conclusion sheets. Parse the SWOT already produced
    # in agent_context (no second LLM call) and synthesise a figures-grounded
    # conclusion, both as ordinary Sheet!Cell answers so template-fill writes them with
    # everything else. Non-fatal, and keys find no sheet on templates without the annex.
    try:
        from services.report_annex_service import build_annex_cell_answers
        swot_md = agent_context.split("SWOT:", 1)[1] if "SWOT:" in agent_context else None
        if swot_md and "\n\nVERIFIED" in swot_md:
            swot_md = swot_md.split("\n\nVERIFIED", 1)[0]
        # The market-research and feasibility agents already run for every report, but
        # their output only ever fed the prompt — it was never shown to the reader.
        # Carry them onto the model so the report can present them as real sections.
        def _agent_part(name, until):
            if name not in agent_context:
                return None
            body = agent_context.split(name, 1)[1]
            for stop in until:
                if stop in body:
                    body = body.split(stop, 1)[0]
            body = body.strip()
            return body if len(body) > 40 and "(unavailable" not in body[:40] else None

        market = _agent_part("MARKET RESEARCH:", ("FEASIBILITY:", "SWOT:", "\n\nVERIFIED"))
        feasibility = _agent_part("FEASIBILITY:", ("SWOT:", "\n\nVERIFIED"))
        if market:
            model["market_research"] = market
        if feasibility:
            model["feasibility_analysis"] = feasibility
        annex = build_annex_cell_answers(project, get_config(purpose_key)["label"], model, swot_md)
        if annex:
            answers.update(annex)
            _persist_answers(db, project, answers)
    except Exception:
        logger.warning("generate: workbook annex build failed", exc_info=True)

    # Prefer the exact, recalculated headline KPIs for template-fill purposes so the
    # Word/preview figures stay consistent with the Excel model.
    if derived_kpis:
        model["kpis"] = derived_kpis
    if consistency is not None:
        model["consistency_checks"] = consistency
    # The 5-year figures read straight from the recalculated Excel — the Word report
    # renders these, so its numbers are the Excel's numbers, not a separate estimate.
    if excel_summary:
        model["financial_summary"] = excel_summary
    if market_segments:
        model["market_segments"] = market_segments
    if statement_tables:
        model["statement_tables"] = statement_tables
    if key_assumptions:
        model["key_assumptions"] = key_assumptions

    # Where the market and industry content came from. Skipped for excel_only, which asks
    # for the workbook alone and pays for no prose. Never fatal — a report without a
    # reference list is still a report.
    if not req.excel_only:
        try:
            from agents.references_agent import references_agent
            refs = references_agent(_project_dict(project, answers))
            if refs:
                model["references"] = refs
        except Exception:
            logger.warning("generate: references unavailable", exc_info=True)

    preview = _preview_markdown(model, purpose_key, project, excel_only=req.excel_only)

    # Persist on the project's report row.
    report = project.report
    if not report:
        report = Report(project_id=project.id)
        db.add(report)
    report.report_content = preview
    report.financial_model = json.dumps(model)
    report.financial_format = purpose_key
    report.status = "completed"
    project.status = "completed"
    db.commit()

    return {
        "purpose": purpose_key,
        "purpose_label": get_config(purpose_key)["label"],
        "report_content": preview,
        "kpis": model.get("kpis", []),
        "consistency_checks": model.get("consistency_checks", []),
        "sheet_names": [s.get("name") for s in model.get("sheets", [])],
        # So the UI can tell the user why there is no written report, and offer the
        # "generate the report too" action instead of looking broken.
        "excel_only": req.excel_only,
    }


def _is_short(project: Project) -> bool:
    """Did the client ask for the SHORT format?

    `report_format` has been collected on the create screen since the beginning and stored
    on both the project and the report — and then never read, so "Short" and "Long" produced
    byte-identical deliverables. It decides the deliverables now.
    """
    return str(getattr(project, "report_format", "") or "").strip().lower() == "short"


@router.get("/{project_id}/excel")
def download_excel(project: Project = Depends(get_owned_project), db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    _require(may_export(current_user, "excel"))
    purpose_key = resolve_purpose(project.purpose, project.financial_format)
    answers = _stored_answers(db, project)

    # The short format is a different DELIVERABLE, not a different model: the two-sheet
    # workbook is written from the same stored figures the full one is, so a client who
    # later asks for the long report finds the same numbers, not a second opinion.
    if _is_short(project):
        from services.short_report import build_short_excel
        try:
            data = build_short_excel(_load_model(project), _project_dict(project, answers))
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("excel: short workbook failed for project %s", project.id)
            raise HTTPException(status_code=502, detail=f"Short workbook failed: {e}")
        return StreamingResponse(
            BytesIO(data), media_type=XLSX_MIME,
            headers={"Content-Disposition":
                     f'attachment; filename="{_slug(project.title)}_overview.xlsx"'})

    # Ensure the workbook annex (SWOT + Conclusion) is populated even for projects
    # generated before the annex existed: if the stored answers carry no SWOT cells,
    # build them now from the stored model and the SWOT agent. Non-fatal.
    if not any(k.startswith("SWOT!") for k in answers):
        try:
            from services.report_annex_service import build_annex_cell_answers
            model = None
            if project.report and project.report.financial_model:
                try:
                    model = json.loads(project.report.financial_model)
                except (ValueError, TypeError):
                    model = None
            annex = build_annex_cell_answers(project, "", model)
            if annex:
                answers = {**answers, **annex}
                _persist_answers(db, project, answers)
        except Exception:
            logger.warning("excel: annex backfill failed for project %s", project.id, exc_info=True)

    tpurpose, template = _resolve_template(purpose_key, answers, project.industry, project.purpose)
    # Only use template-fill when the sample workbook actually exists on disk.
    # If the samples were removed, fall through to the deterministic formula-driven
    # model (build_model_excel), which needs no sample and never 502s.
    if template and template_path(tpurpose, template["id"]):
        # Template-fill: hand back the real sample workbook with the user's
        # inputs written into its input cells; every formula/chart is preserved
        # and Excel recomputes the whole model on open.
        logger.info("excel: project=%s purpose=%s -> template-fill %s/%s",
                    project.id, purpose_key, tpurpose, template["id"])
        # Run the same deterministic guard chain the generation ran. Without it a
        # download rebuilt the workbook from raw stored cells and could differ from the
        # generated model — an older project's revenue streams came out as zero. Free
        # (no LLM) and idempotent, so a download stays a read-only operation. Scoped to
        # the industry workbooks — see _reconcile_on_read.
        if _reconcile_on_read(template):
            answers = _reconcile_all(answers, project, template)
        try:
            data = optional_sheets.apply(
                fill_template(tpurpose, template["id"], answers), answers)
        except Exception as e:
            # fill_template validates the workbook and raises on any corruption,
            # so we return an error rather than ever streaming a broken file.
            logger.exception("excel: template fill failed for project %s", project.id)
            raise HTTPException(status_code=502, detail=f"Template fill failed: {e}")
        # Serve the RECALCULATED workbook so it already carries computed values, not
        # just formulas. Non-fatal: if LibreOffice is unavailable we serve the filled
        # file and Excel recomputes on open (fullCalcOnLoad is set).
        if libreoffice_available():
            try:
                data = recalculate_xlsx(data)
            except Exception:
                logger.warning("excel: server recalc failed; serving filled workbook", exc_info=True)
        fname = template_filename(tpurpose, template["id"], f"{_slug(project.title)}_financial_model")
        media = XLSM_MIME if fname.endswith(".xlsm") else XLSX_MIME
        logger.info("excel: streaming %s (%d bytes, %s)", fname, len(data), media)
        return StreamingResponse(BytesIO(data), media_type=media,
                                 headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    if has_sample(purpose_key):
        # Sample-driven purpose (no template-fill template): mirror the sample's
        # structure using the LLM-built model.
        model = _load_model(project)
        data = build_excel(model, purpose_key, _project_dict(project, {}))
    else:
        # Deterministic, fully formula-driven model. Assumptions are derived from
        # the project + questionnaire answers; every other cell is an Excel formula.
        data = build_model_excel(_project_dict(project, answers))

    fname = f"{_slug(project.title)}_financial_model.xlsx"
    return StreamingResponse(BytesIO(data), media_type=XLSX_MIME,
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


def _build_word_report(project: Project, db: Session):
    """The Word report bytes + base filename. For an uploaded Word (.docx) template
    this fills its {{placeholders}}; otherwise it builds the narrative report from the
    stored model (whose KPIs are the server-recalculated, consistent values)."""
    purpose_key = resolve_purpose(project.purpose, project.financial_format)
    answers = _stored_answers(db, project)
    tpurpose, template = _resolve_template(purpose_key, answers, project.industry, project.purpose)
    if template and template.get("engine") == "docx_fill" and template_path(tpurpose, template["id"]):
        from services.docx_fill_service import fill_docx
        ctx = {f: getattr(project, f, None) for f in _PROJECT_FIELDS}
        for k, v in answers.items():
            # answer keys are "Sheet!Cell"; docx placeholder names carry no sheet
            ctx[k.split("!", 1)[1] if "!" in k else k] = v
        data = fill_docx(template_path(tpurpose, template["id"]), ctx)
        return data, f"{_slug(project.title)}_{template['id']}"
    model = _load_model(project)

    # Three pages instead of fifty, for a client who is still deciding whether to do this
    # at all. Same stored model — see the note on the short workbook above.
    if _is_short(project):
        from services.short_report import build_short_word
        return (build_short_word(model, _project_dict(project, answers)),
                f"{_slug(project.title)}_overview")

    # Mirror the workbook's SWOT + Conclusion into the Word report from the same
    # stored annex cells, so the two documents carry identical narrative.
    swot = {}
    for sec, cell in (("strengths", "SWOT!B6"), ("weaknesses", "SWOT!D6"),
                      ("opportunities", "SWOT!B8"), ("threats", "SWOT!D8")):
        v = answers.get(cell)
        if isinstance(v, str) and v.strip():
            swot[sec] = v
    if swot:
        model["swot"] = swot
    concl = answers.get("Conclusion!B28")
    if isinstance(concl, str) and concl.strip():
        model["conclusion"] = concl
    # Reports generated before these sections existed carry neither. Build them ONCE, on
    # the first download, and persist — so this is not a per-download LLM call, and an
    # existing report gains them without a full regeneration (which would take minutes and
    # recompute every figure). Both are written from the stored model's own numbers.
    #
    # `setdefault`, NOT `model.get("narrative") or {}`: an empty narrative is FALSY, so that
    # form handed back a brand-new dict and every section written into it was thrown away.
    # A workbook-only run stores exactly that empty narrative, which is the case this has to
    # work for.
    narrative = model.setdefault("narrative", {})
    from agents.business_model_agent import EXEC_MIN_WORDS
    # The References, the Business Model and the Executive Summary are all self-contained —
    # each is written from the project record and the recalculated figures, not from the
    # narrative — and all three run on the CHEAP model, seconds each. So they are filled in
    # even for a workbook-only report, where they are the difference between a usable Word
    # file and an empty one. What the expensive narrative call still owns is the section
    # commentary (Borrower & Banking, Financial Statements Overview, Working Capital, Ratio
    # Analysis, Loan Assessment); that is what the "Write the Word report too" toggle buys.
    exec_words = len(str(narrative.get("Executive Summary") or "").split())
    exec_needed = exec_words < EXEC_MIN_WORDS
    if not model.get("references") or not narrative.get("Business Model") or exec_needed:
        pd = _project_dict(project, answers)
        try:
            if not narrative.get("Business Model"):
                from agents.business_model_agent import business_model_agent
                bm = business_model_agent(pd, model.get("financial_summary"))
                if bm:
                    narrative["Business Model"] = bm
            if exec_needed:
                # Written from scratch when there is none (a workbook-only report), or
                # expanded when the stored one is under half a page. Either way it runs
                # ONCE and is persisted, so it is not rewritten on each download — which
                # would also make two downloads of the same report differ.
                from agents.business_model_agent import exec_summary_agent
                es = exec_summary_agent(pd, model.get("financial_summary"),
                                        narrative.get("Executive Summary") or "")
                if es:
                    narrative["Executive Summary"] = es
            if not model.get("references"):
                from agents.references_agent import references_agent
                refs = references_agent(pd)
                if refs:
                    model["references"] = refs
            if project.report:
                project.report.financial_model = json.dumps(model)
                db.commit()
        except Exception:
            logger.warning("word: back-fill of new sections failed for project %s",
                           project.id, exc_info=True)
    # The stored answers go in, not an empty dict: the cover reads the bank's name from
    # them, and with {} that line silently never appeared.
    pdict = _project_dict(project, answers)
    pdict["branding"] = _branding(db, project)      # logo bytes + brand colour
    pdict["section_inserts"] = _section_inserts(answers)
    data = build_word(model, purpose_key, pdict)
    return data, f"{_slug(project.title)}_{purpose_key}"


@router.get("/{project_id}/word")
def download_word(project: Project = Depends(get_owned_project), db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    _require(may_export(current_user, "word"))
    try:
        data, base = _build_word_report(project, db)
    except Exception as e:
        logger.exception("word: report build failed for project %s", project.id)
        raise HTTPException(status_code=502, detail=f"Word report failed: {e}")
    return StreamingResponse(BytesIO(data), media_type=DOCX_MIME,
                             headers={"Content-Disposition": f'attachment; filename="{base}.docx"'})


@router.get("/{project_id}/pdf")
def download_pdf(project: Project = Depends(get_owned_project), db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    _require(may_export(current_user, "pdf"))
    """PDF of the report, rendered by LibreOffice from the SAME Word document that the
    .docx download produces — so the PDF, Word and Excel all carry identical figures."""
    from services.recalc_service import to_pdf, libreoffice_available
    if not libreoffice_available():
        raise HTTPException(
            status_code=503,
            detail="PDF export needs LibreOffice on the server (set LIBREOFFICE_PATH).")
    try:
        word, base = _build_word_report(project, db)
        data = to_pdf(word, "docx")
    except Exception as e:
        logger.exception("pdf: export failed for project %s", project.id)
        raise HTTPException(status_code=502, detail=f"PDF export failed: {e}")
    return StreamingResponse(BytesIO(data), media_type="application/pdf",
                             headers={"Content-Disposition": f'attachment; filename="{base}.pdf"'})

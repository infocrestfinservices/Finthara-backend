"""
template_model_service.py

Generates the user's OWN financial model as a set of input-cell values for a
chosen sample template. The sample is a DESIGN BLUEPRINT only — we never keep its
business numbers. The AI reads all the chatbot/questionnaire answers, computes
realistic assumptions for THIS business, and returns one value per input cell.
Those values are written into the template's input cells; the template's own
formulas then recompute every dependent sheet, chart, KPI and ratio.

So the division of labour is:
  - AI  -> the input assumptions/drivers for the user's business
  - the sample's formulas -> every derived statement (P&L, cash flow, IRR, …)

This keeps the model mathematically consistent (the template guarantees it) while
guaranteeing the output represents the user's business, not the sample's.
"""

import json
import logging
import re

from services.claude_service import invoke_llm
from services.financial_model_service import _extract_json
from services.template_introspect import load_schema
from services.template_fill_service import field_key

logger = logging.getLogger("template_model")

# Cap how many input cells we ask the model to fill in one shot (very large
# templates are chunked so the request stays reliable).
_MAX_FIELDS_PER_CALL = 90


def _project_summary(project: dict) -> str:
    """A compact, complete dump of everything we know about the user's business."""
    answers = project.get("purpose_answers") or {}
    # Drop internal control keys from the answers we show the model.
    answers = {k: v for k, v in answers.items() if "!" not in k and not k.startswith("_")}
    lines = [
        f"Business name: {project.get('title')}",
        f"Industry: {project.get('industry')} / {project.get('sub_industry')}",
        f"Country / currency: {project.get('country')} / {project.get('currency')}",
        f"Location: {project.get('location')}",
        f"Promoter: {project.get('promoter_name')} ({project.get('promoter_experience')})",
        f"Description: {project.get('project_description')}",
        f"Target market: {project.get('target_market')}; customers: {project.get('target_customers')}",
        f"Total project cost: {project.get('project_cost')}",
        f"Own contribution / equity: {project.get('own_contribution')}",
        f"Loan / funding: {project.get('loan_amount')}",
        f"Purpose: {project.get('purpose')}",
    ]
    if answers:
        lines.append("Questionnaire answers: " + json.dumps(answers))
    return "\n".join(str(x) for x in lines)


def _fields_for_prompt(schema: dict):
    """Flatten the schema into (key, label, type, sample_example, hint, options)."""
    out = []
    for g in schema.get("groups", []):
        sheet = g["sheet"]
        for f in g.get("fields", []):
            out.append((
                field_key(sheet, f["cell"]),
                f.get("label", ""),
                f.get("type", "number"),
                f.get("default"),
                f.get("hint", ""),
                f.get("options") or [],
            ))
    return out


def _prompt(project: dict, agent_context: str, fields, currency: str,
            constraints: str = "") -> str:
    field_lines = []
    for key, label, ftype, sample, hint, options in fields:
        if ftype == "percent":
            unit = "decimal fraction e.g. 0.15 for 15%"
        elif ftype == "text":
            unit = "short free text"
        elif ftype == "enum":
            # The template's own dropdown. Its lookups key off this exact string, so
            # a near-miss ("Retail") silently breaks every industry-driven label.
            unit = ("choose EXACTLY ONE of these, copied verbatim (character for "
                    "character): " + " | ".join(options))
        else:
            unit = ftype
        ctx = f" | basis: {hint}" if hint else ""
        field_lines.append(
            f'- "{key}" | {label} | unit: {unit}{ctx} | sample_example(DO NOT REUSE): {sample}')

    return f"""You are a senior Chartered Accountant and financial modeller building the input assumptions for a client's project financial model.

CLIENT BUSINESS (use ALL of this):
{_project_summary(project)}

SUPPORTING ANALYSIS (context; do not contradict):
{(agent_context or '')[:2500]}

Below is the list of INPUT cells of a professional financial-model template. Each line shows a stable key, a human label, the expected unit, and a sample_example value from an UNRELATED sample business. The sample_example shows ONLY the format/scale/units — it is from a different business and you must NEVER reuse it.

Produce a realistic, internally-consistent value for EVERY key, computed for the CLIENT'S business described above. Base values on the client's stated numbers where given (cost, capacity, prices, financing, tax, growth, etc.), and estimate sensible industry-appropriate figures for anything not stated. Use the "basis" note on each cell to keep values in a realistic band. Monetary values are in {currency}. Percentages MUST be decimal fractions (0.15, not 15). Years/counts are plain integers. "short free text" cells take a brief string (e.g. the unit's name or plant location).

MODEL VIABILITY — THIS IS CRITICAL. The output MUST be a VIABLE, BANKABLE business, not a loss-making one. Before returning, mentally check the arithmetic the template will do:
  * ANNUAL REVENUE = volume (capacity/transactions/units) × selling price. Size the VOLUME and PRICE so revenue is large enough to cover ALL costs and leave a profit.
  * TOTAL ANNUAL COSTS = per-unit variable costs × volume  +  12 × each monthly fixed cost (wages, overheads, admin, rent, etc.)  +  depreciation  +  interest. The SUM of these MUST be COMFORTABLY LESS than revenue.
  * The business must show a POSITIVE EBITDA and a healthy EBITDA margin (typically 10–30% of revenue for the industry) from YEAR 1, and positive profit after tax by Year 1 or Year 2 at the latest.
  * It must service its debt: yearly (profit after tax + depreciation + interest) must be at least 1.5× the yearly loan repayment (DSCR ≥ 1.5).
  * Monthly fixed costs are PER MONTH — do not set them so high that 12× them exceeds a large share of revenue. A tiny business with huge salaries is not viable; scale fixed costs to the revenue.
If your first pass would make a loss, INCREASE the volume/price or REDUCE the costs until the business is clearly profitable and bankable. Never return a model where costs exceed revenue.

TARGET MARKET SEGMENTS — if the field list contains "Segment N — Segment name" / "Segment N — Share of revenue" cells, split the revenue across the CLIENT'S OWN stated target market and customers (quoted above). Name each segment in the client's words, not generic ones (e.g. for a dermatology clinic: "Walk-in OPD patients", "Referral patients", "Insurance / corporate tie-ups"; for a shop: "Walk-in retail", "Bulk / institutional", "Online orders"). Use as many of the five rows as the business genuinely has and leave the rest blank. The shares MUST be decimal fractions that ADD UP TO EXACTLY 1.0 across the segments you fill, and the biggest segment should reflect where the client said most of their business comes from.
{("MODEL CONSISTENCY REQUIREMENTS (obey exactly): " + constraints) if constraints else ""}

INPUT CELLS:
{chr(10).join(field_lines)}

Return ONLY a single JSON object mapping each key to its value, nothing else:
{{ "Sheet!Cell": <number or short string>, ... }}
Rules:
- Include every key listed above.
- Numeric cells are plain numbers (no currency symbols, commas or text); free-text cells are short strings.
- Never output the sample_example values; compute for the client's business.
- Keep the model consistent AND profitable: financing = equity + loan ≈ project cost; revenue exceeds total costs every year; positive EBITDA and DSCR ≥ 1.5; a volume ramp that starts lower and rises."""


def _as_number(v):
    """A number from a plainly-numeric string ("6,00,000", "12%", "₹ 4500"), else None."""
    if not isinstance(v, str):
        return None
    s = v.strip().replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "").strip()
    pct = s.endswith("%")
    s = s.rstrip("%").strip()
    try:
        n = float(s)
    except (TypeError, ValueError):
        return None
    if pct:
        n /= 100.0
    return int(n) if float(n).is_integer() else n


def _norm_option(s: str) -> str:
    """Fold an option to its comparable core: case, spacing and punctuation carry no
    meaning here ("retail & ecommerce" IS "Retail & E-Commerce"), so drop them."""
    return re.sub(r"[^a-z0-9]", "", str(s).casefold())


def _match_option(value, options: list):
    """The option the model meant, or None. Exact first, then punctuation/case
    insensitive, then a UNIQUE substring hit — the template's lookups need the
    verbatim string, so anything ambiguous is rejected rather than guessed."""
    v = str(value).strip()
    if v in options:
        return v
    key = _norm_option(v)
    if not key:
        return None
    norm = {_norm_option(o): o for o in options}
    if key in norm:
        return norm[key]
    near = [o for o in options if key in _norm_option(o) or _norm_option(o) in key]
    return near[0] if len(near) == 1 else None


def _generate_chunk(project, agent_context, fields, currency, constraints="") -> dict:
    # settings.CELLFILL_HEAVY decides which model fills the input cells — see config.py.
    # Read at call time, not at import, so flipping it in .env takes effect on reload.
    from config import settings
    raw = invoke_llm(_prompt(project, agent_context, fields, currency, constraints),
                     heavy=settings.CELLFILL_HEAVY)
    data = _extract_json(raw)
    types = {k: ftype for (k, _, ftype, _, _, _) in fields}
    opts = {k: o for (k, _, _, _, _, o) in fields}
    out, dropped = {}, []
    for k, v in (data or {}).items():
        ftype = types.get(k)
        if ftype is None:
            continue
        if ftype == "enum":
            m = _match_option(v, opts.get(k) or [])
            if m:
                out[k] = m
            else:
                dropped.append(f"{k}(enum, no option matches {v!r})")
        elif ftype == "text":
            if isinstance(v, str) and v.strip():
                out[k] = v.strip()
            else:
                dropped.append(f"{k}(text, got {type(v).__name__})")
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
        else:
            # A number cell the model answered with text. Salvage a figure if the
            # string is plainly numeric ("6,00,000", "12%"); never write a guess.
            num = _as_number(v)
            if num is None:
                dropped.append(f"{k}({ftype}, got {v!r})")
            else:
                out[k] = num
    if dropped:
        # Silence here is how blank names and an unset industry reached the workbook:
        # every mismatch was discarded without a trace.
        logger.warning("AI input generation dropped %d value(s): %s",
                       len(dropped), "; ".join(dropped[:12]))
    return out


def _fmt_kpi(val, ftype: str) -> str:
    if ftype == "percent":
        return f"{val * 100:.1f}%"
    if isinstance(val, (int, float)) and float(val).is_integer():
        return f"{val:,.0f}"
    return f"{val:,.2f}"


def derive_headline_kpis(schema: dict, answers: dict) -> list:
    """Headline KPIs computed DIRECTLY from the template's input cells, so the Word
    report can never quote a figure that disagrees with the (editable) Excel model.
    Only exact, input-derived numbers are shown here; genuinely template-computed
    metrics (DSCR, IRR) are surfaced as a pointer to the recomputed Excel sheets."""
    out = []
    for sp in schema.get("headline_kpis", []) or []:
        label = sp.get("label", "")
        ftype = sp.get("type", "number")
        if "ref" in sp:
            out.append({"label": label, "value": sp["ref"]})
        elif "cell" in sp:
            v = answers.get(sp["cell"])
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append({"label": label, "value": _fmt_kpi(v, ftype)})
        elif "sum" in sp:
            vals = [answers.get(c) for c in sp["sum"]]
            vals = [x for x in vals if isinstance(x, (int, float)) and not isinstance(x, bool)]
            if vals:
                out.append({"label": label, "value": _fmt_kpi(sum(vals), ftype)})
        elif "ratio" in sp:
            a, b = answers.get(sp["ratio"][0]), answers.get(sp["ratio"][1])
            if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b:
                out.append({"label": label, "value": f"{a / b:.2f} : 1"})
    return out


def _industry_profile_context(project: dict) -> str:
    """Ground the AI fill in the SELECTED industry's real economics using its frozen
    profile (financial_engine/industry_profiles). Without this the model is told only
    "estimate industry-appropriate figures" and fills a generic factory — the reason a
    retail model came back looking like manufacturing. Returns '' when no profile
    exists for the industry (falls back to the generic prompt, unchanged)."""
    industry = (project.get("industry") or "").strip()
    if not industry:
        return ""
    try:
        from financial_engine.industry_profiles.profile import available, load_profile
    except Exception:
        return ""

    def fold(s):
        return re.sub(r"[^a-z0-9]", "", str(s or "").casefold())

    want = fold(industry)
    prof = None
    for slug in available():
        p = load_profile(slug)
        if not p:
            continue
        if fold(p.display_name) == want or fold(slug) == want or want in fold(p.display_name):
            prof = p
            break
    if not prof:
        return ""

    lines = [
        f"INDUSTRY MODEL — {prof.display_name} (obey this; it defines how THIS industry works):",
        f"Revenue model: {prof.revenue_model}",
    ]
    if prof.notes:
        lines.append("Structural facts about this industry:")
        lines += [f"  - {n}" for n in prof.notes[:5]]
    # This is the TEMPLATE-fill path: the workbook reuses its capacity/cost cells with
    # industry-specific meaning (the labels change per industry), so the AI must fill
    # them for THIS industry — never zero the volume or price, or revenue collapses.
    lines.append(
        "IMPORTANT — this template reuses a factory layout for every industry, so read "
        "the input cells in THIS industry's terms:")
    lines.append(
        "  - The 'installed capacity / production' cell is this business's annual SALES "
        "VOLUME (units sold or purchased for resale). Fill a realistic non-zero volume; "
        "revenue = volume × price, so it must not be 0.")
    lines.append(
        "  - The 'raw material cost per unit' cell is this industry's main per-unit cost "
        "of goods (for trading/retail, the purchase cost of the item, typically the price "
        "minus the gross margin). The 'second/power cost per unit' cell is the next "
        "per-unit variable cost THIS INDUSTRY actually incurs — power & fuel for a hotel "
        "or factory, kitchen fuel for a restaurant, packaging & freight for a trader. Fill "
        "a realistic non-zero figure for it in almost every case; leave it 0 ONLY when the "
        "cost genuinely does not exist for this specific business (e.g. a pure software "
        "company has no packaging cost). Never leave this at 0 while giving it a non-zero "
        "escalation rate — an escalating cost that starts at zero stays zero forever, which "
        "is never what an escalation rate means.")
    lines.append(
        "  - Skip genuine factory-only overheads (factory overhead, plant & machinery) "
        "only where the business truly has none.")
    # a few high-signal bands so the figures land in the right zone
    bands = []
    for key in ("gross_margin_pct", "selling_price_y1", "receivables_days", "payables_days",
                "wc_margin_pct"):
        r = prof.rule(key)
        if r.applies and r.band:
            bands.append(f"{r.label or key} ~ {r.band[0]}–{r.band[1]}")
    if bands:
        lines.append("Typical ranges: " + "; ".join(bands) + ".")
    return "\n".join(lines)


def generate_template_inputs(project: dict, purpose_key: str, template_id: str,
                             agent_context: str = "") -> dict:
    """Return {"Sheet!Cell": value} for every input cell of the template, computed
    for the user's business. Returns {} if the template has no schema."""
    schema = load_schema(purpose_key, template_id)
    if not schema:
        logger.warning("no schema for %s/%s; cannot AI-generate inputs", purpose_key, template_id)
        return {}

    fields = _fields_for_prompt(schema)
    currency = project.get("currency") or "INR"
    constraints = schema.get("constraints", "")
    # Prepend the selected industry's profile so the fill reflects THAT industry, not a
    # generic factory. Non-destructive: only changes the values the AI proposes.
    ind_ctx = _industry_profile_context(project)
    if ind_ctx:
        agent_context = (ind_ctx + "\n\n" + (agent_context or "")).strip()
        logger.info("industry profile context applied for '%s'", project.get("industry"))
    result = {}
    # Chunk large templates so each request stays within a reliable size.
    for i in range(0, len(fields), _MAX_FIELDS_PER_CALL):
        chunk = fields[i:i + _MAX_FIELDS_PER_CALL]
        try:
            result.update(_generate_chunk(project, agent_context, chunk, currency, constraints))
        except Exception as e:
            logger.warning("AI input generation failed for chunk %d: %s", i, e)
    logger.info("AI generated %d/%d input values for %s/%s",
                len(result), len(fields), purpose_key, template_id)
    return result

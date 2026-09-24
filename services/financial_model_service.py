"""
financial_model_service.py

Builds a purpose-specific prompt that instructs the LLM to act as a Chartered
Accountant and return a STRICT JSON financial model (no narrative prose outside
the JSON). The backend then turns that JSON into Word + Excel.

This does NOT add a new agent — it composes the existing agents' output
(market / feasibility / swot) plus the questionnaire answers into one
structured-output call.
"""

import json
from services.claude_service import invoke_llm
from purpose_config import get_config


def _fmt_money(v, currency: str = "INR") -> str | None:
    """Indian lakh/crore digit grouping with a currency symbol, for figures quoted
    inline in the narrative. Feeding the model an already-formatted headline figure
    is more reliable than asking it to apply Indian-style grouping itself -- it was
    writing bare digits like "8500000" straight into bullets when only told the
    JSON's own numbers must be plain (a rule left over from when this prompt also
    returned full numeric sheets; see the "sheets" comment further down)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if currency != "INR":
        return f"{currency} {v:,.0f}"
    neg = v < 0
    digits = str(int(round(abs(v))))
    if len(digits) <= 3:
        grouped = digits
    else:
        last3, rest = digits[-3:], digits[:-3]
        parts = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        grouped = ",".join(parts) + "," + last3
    return f"{'-' if neg else ''}₹{grouped}"


def _required_headers(config) -> dict:
    """For each sheet, collect the column headers the dashboard charts need so
    the AI is told to include them."""
    needed = {}
    for ch in config.get("charts", []):
        sheet = ch["sheet"]
        cols = needed.setdefault(sheet, set())
        cols.add(ch["x"])
        for s in ch["series"]:
            cols.add(s)
    return {k: sorted(v) for k, v in needed.items()}


def build_prompt(project: dict, purpose_key: str, agent_context: str,
                 sample_blueprint: str = "", user_instructions: str = "",
                 verdict_info: str = "") -> str:
    config = get_config(purpose_key)
    needed = _required_headers(config)

    # What the client asked for in their own words. This is a first-class block, not part
    # of agent_context: that block is labelled "supporting analysis ... do not contradict"
    # and is truncated, so a request buried in it was outranked by the hard output rules
    # below and the report came back unchanged.
    ask = (user_instructions or "").strip()
    ask_block = f"""
CLIENT'S OWN REQUIREMENTS FOR THIS REPORT — HIGHEST PRIORITY:
{ask}

These are instructions from the person the report is for. Follow them. They outrank the
default section list and the default emphasis. If they ask for content that does not fit
any required section, ADD a new narrative section for it (see the narrative rules below).
The one thing you must never do is bend a number to satisfy them: if what they want cannot
be shown from the figures, say so plainly in the report instead of inventing it.
""" if ask else ""

    # The workbook's own bankability verdict, computed from the actual recalculated DSCR.
    # Same reason this is not folded into agent_context as ask_block above: that block is
    # truncated to 4000 chars, and this instruction — appended after market research,
    # feasibility and SWOT — never survived the cut (5-report audit bug B: the narrative
    # kept quoting its own invented DSCR figures and calling a REVIEW-REQUIRED report
    # "satisfactory" even after the instruction was added, because it never reached the
    # model at all).
    verdict_block = f"""
THE WORKBOOK'S OWN BANKABILITY VERDICT — NON-NEGOTIABLE:
{verdict_info}

Never state a specific DSCR number anywhere in the narrative (not even "about" or
"approximately" one) — say "refer to the DSCR schedule in the Excel model" instead. Your
narrative's tone must match the verdict above exactly: if it is REVIEW REQUIRED or BELOW
NORM, do not call the project "financially viable", "satisfactory", "comfortably
serviceable" or similar — state plainly that the figures need verification / coverage
falls short, consistent with the verdict.
""" if (verdict_info or "").strip() else ""

    sample_block = ""
    if sample_blueprint:
        sample_block = f"""
SAMPLE REPORT BLUEPRINT — this is the reference template for this PURPOSE. Treat it as the blueprint for structure, sheet organisation, column names, layout, number formats and calculation logic. Reproduce the SAME structure and presentation, but:
- Use ONLY the user's data above; NEVER copy the sample's actual values.
- Recompute every figure from the user's inputs, consistent with the sample's methodology.
- Where the user hasn't supplied a value, infer a realistic one consistent with their business and note it as an assumption.
- Your "sheets" output should mirror the sample's sheets, columns and row structure (same names/order where sensible).

{sample_blueprint}
"""

    # The model used to be asked for a full "sheets" array as well — roughly 40% of the
    # output tokens. Nothing read it except the on-screen preview's statement tables, and
    # that screen no longer shows them: the WORKBOOK comes from the filled template and the
    # Word report reads its figures from that recalculated workbook, never from here. So it
    # is no longer requested, which makes the one paid call meaningfully cheaper and faster.
    # `config["excel_sheets"]` still drives the template side; it is simply not prompted for.

    # "Business Model" is required of EVERY purpose, not listed per-purpose: a lender reads
    # it to understand what they are lending against before any projection means anything.
    # It is rendered near the front of the report, straight after the executive summary.
    section_specs = [f'- "{w["title"]}": {w["guidance"]}' for w in config["word_sections"]]
    # The summary opens the report and used to run to under half a page, leaving white
    # space where a credit officer expects the whole case. It is the one section a reader
    # may read alone, so it is held to a length, not left to "concise".
    section_specs.append(
        '- "Executive Summary": must cover A FULL PAGE of content — the business and what '
        'it proposes to do, the promoter, the market and demand, the cost of the project '
        'and how it is funded, the projected results and what they mean for viability, the '
        'coverage available to the lender, and the risks with their mitigation. Structure: '
        'break it into clearly-labelled topic groups (a short plain-text label line per '
        'topic — "Business & Promoter", "Market & Demand", "Project Cost & Funding", '
        '"Projected Results", "Coverage", "Key Risks"), each opening with a proper paragraph '
        '(several full sentences, not a one-liner) that actually explains that topic, '
        'followed by 2-4 bullet points that pull out the specific figures worth scanning '
        'separately (see the STRUCTURE rule below on how to write these label lines and on '
        'paragraph-then-bullets ordering). A reader who reads only this page must understand '
        'the whole proposal; a page of nothing but bullet fragments is not more readable, it '
        'reads as a list of disconnected facts instead of the case being argued.')
    section_specs.append(
        '- "Business Model": REQUIRED. Explain in full how this specific business makes '
        'money — what exactly is sold and to whom, the revenue streams and roughly what '
        'share each contributes, how it is priced, the channels and how customers are won, '
        'the cost structure (what is fixed, what varies with volume), the working-capital '
        'cycle (who pays when, what stock is held), the key operating drivers the profit '
        'depends on, and what makes the model defensible. Write it about THIS business '
        'using its own numbers and inputs, not a textbook description of the industry. '
        'Structure: a substantial paragraph per sub-topic (see the STRUCTURE rule below — '
        'aim for at least 8 lines of real prose, not a one-line teaser), then bullet points '
        'afterward for the supporting specifics.')
    answers = project.get("purpose_answers") or {}

    currency = project.get("currency") or "INR"

    def _hf(key):
        v = project.get(key)
        return _fmt_money(v, currency) or "N/A"

    return f"""You are a senior Chartered Accountant and financial modeller. You do NOT use a fixed template — you first consider the REPORT PURPOSE below, decide the correct financial-modelling methodology and reporting standard for it, and then produce the model.
{ask_block}{verdict_block}
REPORT PURPOSE: {config['label']}
INDUSTRY: {project.get('industry') or 'N/A'}  (sub: {project.get('sub_industry') or 'N/A'})
COUNTRY / CURRENCY: {project.get('country') or 'N/A'} / {currency}
BUSINESS: {project.get('title') or 'N/A'}
PROMOTER: {project.get('promoter_name') or 'N/A'} — {project.get('promoter_experience') or 'N/A'}
DESCRIPTION: {project.get('project_description') or 'N/A'}
TARGET MARKET: {project.get('target_market') or 'N/A'}
HEADLINE FINANCES: project_cost={_hf('project_cost')}, own_contribution={_hf('own_contribution')}, loan={_hf('loan_amount')}
(quote these exact formatted figures verbatim wherever the narrative mentions them — do not strip the commas/symbol back out)

PURPOSE-SPECIFIC ANSWERS (use these as the primary numeric inputs; infer reasonable values for anything missing and state assumptions):
{json.dumps(answers, indent=2)}

SUPPORTING ANALYSIS FROM PRIOR AGENTS (use for narrative, do not contradict):
{agent_context[:4000]}
{sample_block}
Produce a complete, internally-consistent model. The PURPOSE-SPECIFIC ANSWERS above are given as plain numbers for you to read; use realistic CA-grade figures derived from the inputs. Projections cover the standard horizon for this purpose (typically 5 years; for CMA use 2 past + 3 projected).

Return ONLY a single JSON object (no markdown, no commentary) with EXACTLY this shape:

{{
  "narrative": {{
{chr(10).join(f'      "{w["title"]}": "<a substantial paragraph of real prose (at least 8 lines) that actually explains the topic, THEN bullet points (lines starting with -) after it for the supporting specifics -- not a one-line teaser followed by a wall of bullets; use \\n for line breaks>," ' for w in config["word_sections"])}
  }},
  "kpis": [ {{ "label": "e.g. IRR / DSCR / Break-even", "value": "e.g. 18.4% / 1.85 / 62%" }} ]
}}

The "narrative" object MUST contain AT LEAST these keys:
{chr(10).join(section_specs)}

You MAY add further narrative keys beyond this list, but ONLY to satisfy the client's own
requirements above. Give any such section a short, self-explanatory title (e.g. "Monthly
Revenue Break-up"); it will be rendered after the standard sections. Add nothing extra if
the client asked for nothing extra.

Rules:{f'''
- The client's own requirements at the top of this prompt take priority over the default
  structure and emphasis. Re-read them before you write the narrative, and make the change
  they asked for visible in the output — do not return the same report you would have
  written without them.''' if ask else ''}
- Do NOT reproduce the financial statements as a TABLE: every schedule and CMA form is
  generated from the workbook itself, so a table written here is paid for twice and can
  only disagree with the model. Quote a figure in a sentence where it makes the point, and
  nothing more.
- STRUCTURE, every section: open with a real paragraph of continuous prose — aim for at
  least 8 lines — that actually explains and argues the topic in full sentences, the way a
  Chartered Accountant would write it up, not a one-line teaser. Only AFTER that paragraph,
  add a short bullet list (lines starting with -, normally 3-6 bullets) to call out the
  specific standout figures or points worth a lender scanning separately. The paragraph
  carries the actual explanation; the bullets that follow it are a supporting recap of the
  specifics, not the primary content — a section that is nothing but short bullet
  fragments back to back reads as a list of disconnected facts, not the case being argued.
  Where a section covers more than one topic, repeat this same pattern (paragraph, then
  bullets) separately for each topic, with a short plain-text label line before each
  topic's paragraph (e.g. "Market & demand:") — do NOT use "**bold**" or "#" markdown for
  these labels, neither renders as intended here (## becomes a real heading and would
  wrongly add every label to the report's Table of Contents; ** is not converted to bold
  and shows up as literal asterisks) — a plain short line is enough to read as a label.
- Numbers you quote are the ones given above. Do not invent others.
- Whenever you write a rupee/currency amount anywhere in the narrative or in a "kpis"
  value — not a day-count, month, percentage, ratio or other non-monetary figure, only
  an actual sum of money — format it in Indian digit grouping with the currency symbol,
  e.g. ₹85,00,000 or ₹1,20,00,000, never as a bare number like 8500000. The HEADLINE
  FINANCES figures above are already given to you pre-formatted this way; reuse them as
  given rather than reformatting your own way.
- You do NOT have an accurate DSCR, IRR or NPV. These require the full multi-year cash-flow
  and repayment-schedule model, which you have not been shown — the loan amount, interest
  rate and tenure above are not enough to compute them correctly, even approximately.
  NEVER state, estimate or imply a specific DSCR/IRR/NPV number anywhere in the narrative,
  including phrases like "about 1.75" or "roughly 2x" — write "refer to the DSCR schedule
  in the accompanying Excel model" instead, every time coverage or return comes up. This
  holds even if a WORKBOOK'S OWN VERDICT is given above: use its words (its tier and
  reasoning), never a number of your own.
- The same applies to multi-year REVENUE: you have not been given the workbook's own
  Net Sales figures (they depend on the capacity/price/escalation build-up computed
  inside the sheet, not shown here), so do not independently project or state a specific
  Year 1-Year 5 revenue figure or range — a report that quoted "1.36 Cr to 2.1 Cr" while
  the workbook's own Net Sales row read something else entirely, in the same document,
  is exactly the failure this rule exists to stop. Describe the revenue TRAJECTORY
  qualitatively instead, and let the actual inputs above decide which way it goes — do
  NOT default to describing growth. If capacity utilisation, volume or price escalate
  upward across the years, say something like "revenue is projected to grow steadily as
  capacity utilisation ramps up over the five years"; if a stream's utilisation, volume or
  price instead falls or stays flat across the years, say so plainly (e.g. "revenue is
  projected to ease over the five years as utilisation declines" or "revenue is expected
  to hold broadly flat") — never phrase a falling or flat trajectory as growth. This
  mirrors the DSCR rule above: describe what the numbers actually show, never what a
  report "normally" says. Use a specific number only if one is explicitly given to you
  above (PURPOSE-SPECIFIC ANSWERS or VERIFIED HEADLINE FIGURES), in which case use that
  exact number and no other.
- Every field above that has a real value — PROMOTER, HEADLINE FINANCES (project_cost,
  own_contribution, loan), or any PURPOSE-SPECIFIC ANSWER — IS provided. Never write that
  the promoter is "not identified", or that the project cost / contribution / loan sought
  are "not quantified", "not provided", "recorded as nil" or similar, when the
  corresponding field above is filled in. That instruction not to reproduce the financial
  statements means quote figures sparingly in prose — it does NOT mean treat the underlying
  data as missing. If a field genuinely reads "N/A", it is fine to note that one specific
  gap — do not generalise it into a claim that covers fields which do have values.
- Stay internally consistent across every section of the narrative: if one section states
  a fact (the promoter's name, their experience, a figure, a feature of how the business
  operates), no other section may imply that same fact is unknown or unavailable.
- Describe only the pricing, operations and features that the inputs above actually show.
  A single flat rate/price is a single flat rate — do not embellish it into tiered,
  dynamic, segmented, seasonal or promotional pricing (or any other mechanism) unless a
  field above actually represents that structure. Writing a richer-sounding business than
  the numbers support is not more professional, it is inaccurate.
- Output must be valid JSON and nothing else."""


def _extract_json(text: str) -> dict:
    """Pull the first complete JSON object out of the model's response."""
    if not text:
        raise ValueError("Empty model response")
    t = text.strip()
    # strip ``` fences
    if "```" in t:
        import re
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t)
        if m:
            t = m.group(1).strip()
    # narrow to outermost braces
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model response")
    return json.loads(t[start:end + 1])


def generate_financial_model(project: dict, purpose_key: str, agent_context: str = "",
                             model: str = "claude_sonnet_4_6", sample_blueprint: str = "",
                             user_instructions: str = "", verdict_info: str = "") -> dict:
    """Return the parsed structured model dict. Raises ValueError on bad output."""
    prompt = build_prompt(project, purpose_key, agent_context, sample_blueprint,
                          user_instructions, verdict_info)
    # heavy=True: this is the ONE prompt that asks for the whole report at once (prose +
    # KPIs + several sheets of JSON). On the cheap reasoning model that request consumes the
    # entire 32 K output budget on reasoning and returns EMPTY content (finish_reason=length),
    # which _extract_json raises "Empty model response" on -> 502 on every generation. The
    # heavy model finishes its reasoning and writes the JSON. Cheap callers (cell-fill,
    # agents) deliberately stay on the flash model.
    raw = invoke_llm(prompt, model=model, heavy=True)
    data = _extract_json(raw)

    # Minimal shape guarantees so downstream builders never crash.
    data.setdefault("narrative", {})
    data.setdefault("kpis", [])
    data.setdefault("sheets", [])
    # Drop malformed sheets.
    clean = []
    for s in data["sheets"]:
        if isinstance(s, dict) and s.get("name") and isinstance(s.get("columns"), list) and isinstance(s.get("rows"), list):
            clean.append(s)
    data["sheets"] = clean
    return data

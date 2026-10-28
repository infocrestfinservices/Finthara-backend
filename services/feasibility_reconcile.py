"""
feasibility_reconcile.py

The deterministic guard run on the Feasibility Study workbook's AI-filled inputs, before
they are written. Feasibility only -- generation_router._reconcile_all calls this for the
feasibility_study_all_industries_* template and for nothing else.

The workbook keeps money in Rs LAKH, except the per-unit price/cost cells (Rs per unit).
The AI is told so, but a model that thinks in rupees writes 4000000 where 40 belongs, and
the whole model comes out a hundred thousand times too big. So:
  1. B3 is set from the project's own industry (never the AI's guess);
  2. any lakh cell holding a rupee-sized value (> 100000) is divided by 100000, and a
     monthly cost whose year (x 12) exceeds the whole project cost is too;
  3. term loan / promoter equity are the client's own figures, converted to lakh;
  4. the Project Cost lines are scaled so they add up to the client's project cost;
  5. the result is validated; if validation fails the inputs are returned unchanged.
"""

from __future__ import annotations

import logging

from services import feasibility_template_io as fio
from services.feasibility_industry_map import workbook_industry

logger = logging.getLogger("feasibility")

TEMPLATE_PREFIX = "feasibility_study_all_industries_"
RUPEES_PER_LAKH = 100000
# A lakh cell above this is a rupee amount (100000 lakh = Rs 1,000 crore on one line).
RUPEE_THRESHOLD = 100000
# A monthly cost is only taken for rupees when it is at least this many "lakh" (Rs 10 crore
# a month): below it, a high payroll on a small project (3 lakh/month against a 15 lakh
# cost) is a real figure, not a unit slip, and must not be shrunk to Rs 3.
MONTHLY_RUPEE_FLOOR = 1000


def _key(name: str) -> str:
    sheet, cell, _ = fio.INPUTS[name]
    return f"{sheet}!{cell}"


INDUSTRY = _key("industry")
TERM_LOAN = _key("term_loan")
EQUITY = _key("promoter_equity")
PROJECT_COST = [_key(n) for n in fio.INPUTS if n.startswith("pc_")]
MONTHLY_LAKH = [_key(n) for n in ("direct_labour_per_month", "admin_salaries_per_month",
                                  "fixed_utility_per_month", "other_admin_per_month")]
# Every Rs-lakh cell. The per-unit cells (B10, B11, B39, B40, B41) are Rs per unit and are
# deliberately NOT here: a selling price of 450 or 250000 per unit is a real price.
LAKH_CELLS = MONTHLY_LAKH + [TERM_LOAN, EQUITY] + PROJECT_COST


def is_feasibility_template(template) -> bool:
    return str((template or {}).get("id") or "").startswith(TEMPLATE_PREFIX)


def _num(v):
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _project_rupees(project, answers: dict, field: str):
    """The client's figure in rupees: the project column, else the plain answer key."""
    v = _num(getattr(project, field, None))
    if v is None or v <= 0:
        v = _num((answers or {}).get(field))
    return v if v is not None and v > 0 else None


def _to_values(answers: dict) -> dict:
    """Workbook cell answers -> feasibility_template_io input names, for validate()."""
    cell_to_name = {_key(n): n for n in fio.INPUTS}
    return {cell_to_name[k]: v for k, v in answers.items() if k in cell_to_name}


def reconcile_feasibility(answers: dict, project) -> dict:
    if not isinstance(answers, dict):
        return answers
    out = dict(answers)

    out[INDUSTRY] = workbook_industry(getattr(project, "industry", None))

    for k in LAKH_CELLS:
        v = _num(out.get(k))
        if v is not None and v > RUPEE_THRESHOLD:
            logger.info("feasibility: %s=%s looks like rupees; /%d -> lakh", k, v, RUPEES_PER_LAKH)
            out[k] = round(v / RUPEES_PER_LAKH, 4)

    loan = _project_rupees(project, answers, "loan_amount")
    own = _project_rupees(project, answers, "own_contribution")
    if loan is not None:
        out[TERM_LOAN] = round(loan / RUPEES_PER_LAKH, 4)
    if own is not None:
        out[EQUITY] = round(own / RUPEES_PER_LAKH, 4)

    cost = _project_rupees(project, answers, "project_cost")
    target = cost / RUPEES_PER_LAKH if cost else (
        (loan or 0) / RUPEES_PER_LAKH + (own or 0) / RUPEES_PER_LAKH) or None

    # A monthly cost larger in a year than the whole project is rupees typed into a lakh
    # cell (45000 for Rs 45,000 a month) -- the > 100000 rule above cannot see those.
    reference = target or sum(_num(out.get(k)) or 0 for k in PROJECT_COST)
    if reference > 0:
        for k in MONTHLY_LAKH:
            v = _num(out.get(k))
            if v is not None and v >= MONTHLY_RUPEE_FLOOR and v * 12 > reference:
                logger.info("feasibility: %s=%s a month exceeds the %.2f lakh project in a "
                            "year; taken as rupees -> lakh", k, v, reference)
                out[k] = round(v / RUPEES_PER_LAKH, 4)

    lines = {k: _num(out.get(k)) or 0.0 for k in PROJECT_COST}
    total = sum(lines.values())
    if target and total > 0 and abs(total - target) > 1e-6:
        factor = target / total
        scaled = {k: round(v * factor, 2) for k, v in lines.items()}
        biggest = max(scaled, key=scaled.get)
        scaled[biggest] = round(scaled[biggest] + target - sum(scaled.values()), 2)
        out.update(scaled)
        logger.info("feasibility: project-cost lines scaled %.2f -> %.2f lakh", total, target)
    elif target and total <= 0:
        logger.warning("feasibility: no project-cost lines to scale to %.2f lakh", target)

    funded = (_num(out.get(TERM_LOAN)) or 0) + (_num(out.get(EQUITY)) or 0)
    cost_lakh = sum(_num(out.get(k)) or 0 for k in PROJECT_COST)
    if abs(funded - cost_lakh) > 0.01:
        logger.warning("feasibility: cost %.2f lakh vs loan+equity %.2f lakh -- funding gap "
                       "left as the client stated it", cost_lakh, funded)

    try:
        fio.validate(_to_values(out), allow_missing=True)
    except Exception as exc:
        logger.warning("feasibility: reconciled inputs failed validation (%s); inputs left "
                       "as they were", exc)
        return answers
    return out

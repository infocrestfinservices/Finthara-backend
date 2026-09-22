"""
financing_reconciler.py

Makes the workbook's money tie to what the CLIENT actually said.

The AI fills the financing cells (term loan, promoter's capital) from the client's
stated figures, but it filled the COST-OF-PROJECT components (land, building, plant &
machinery, furniture) independently — so a project the client costed at ₹3 crore, and
financed with a ₹2.1 crore loan, showed only ₹40 lakh of assets being bought. Sources
did not equal uses, and the first question any CA or banker asks is "where did the rest
of the money go?".

This runs AFTER the AI fill and BEFORE the workbook is filled, and enforces
deterministically (never by asking the model again):

  1. Term loan and promoter's capital are the client's stated numbers, full stop.
  2. The cost-of-project components are scaled proportionally so they add up to the
     cost of project — which, in the CMA convention this workbook follows, is what the
     term loan plus promoter's capital funds. (Working capital is financed separately
     by the WC limit the model computes in Form V, so it is deliberately not part of
     this total.)
  3. If the AI gave no usable breakdown at all, a conventional split is applied so the
     total still ties instead of leaving the assets blank.

Only the CMA-family workbooks use these cells; a template without them is left alone.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("financing_reconciler")

# Cost-of-project cells in the CMA family, with the fallback share of the total used
# only when the AI produced no usable breakdown.
_ASSET_CELLS = [
    ("Assumptions!C42", "land", 0.00),
    ("Assumptions!C43", "building", 0.35),
    ("Assumptions!C45", "plant & machinery / equipment", 0.45),
    ("Assumptions!C47", "furniture & other", 0.20),
]
_LOAN_CELL = "Assumptions!C8"
_EQUITY_CELL = "Assumptions!C9"
_TENURE_CELL = "Assumptions!C11"       # MONTHS
_MORATORIUM_CELL = "Assumptions!C12"   # MONTHS


def _fix_months(out: dict):
    """The tenure and moratorium cells are in MONTHS. The label used to say only
    "Term Loan tenure", so the AI filled 5 — meaning 5 years — and the model repaid a
    ₹35 lakh loan in five months: the term loan vanished from the balance sheet by
    Year 1, cash went ₹25 lakh negative and DSCR collapsed to zero. Anything under a
    year is therefore read as years and converted."""
    t = _num(out.get(_TENURE_CELL))
    if t is not None and 0 < t < 12:
        out[_TENURE_CELL] = int(round(t * 12))
        logger.info("financing: term-loan tenure %.0f looked like YEARS; using %d months",
                    t, out[_TENURE_CELL])
    m = _num(out.get(_MORATORIUM_CELL))
    if m is not None and 0 < m < 4 and (out.get(_TENURE_CELL) or 0) >= 12:
        # a 1-3 "month" moratorium alongside a multi-year loan is usually years too,
        # but this one is genuinely ambiguous — leave it, only clamp the absurd case.
        pass
    if m is not None and m < 0:
        out[_MORATORIUM_CELL] = 0


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def reconcile_financing(answers: dict, project) -> dict:
    """Return `answers` with the financing and cost-of-project cells made consistent
    with the client's stated figures. Mutates nothing the caller owns."""
    if not isinstance(answers, dict) or not answers:
        return answers
    # Only applies to workbooks that actually carry these cells.
    if not any(c in answers for c, _, _ in _ASSET_CELLS) and _LOAN_CELL not in answers:
        return answers

    out = dict(answers)
    loan = _num(getattr(project, "loan_amount", None))
    equity = _num(getattr(project, "own_contribution", None))
    stated_cost = _num(getattr(project, "project_cost", None))

    # 1. the client's own financing figures win
    if loan is not None:
        out[_LOAN_CELL] = loan
    if equity is not None:
        out[_EQUITY_CELL] = equity

    # 1b. repayment period is in months — a years-shaped value destroys the model
    _fix_months(out)

    # 2. what the project has to fund: prefer the stated cost, else what is financed
    funded = None
    if loan is not None or equity is not None:
        funded = (loan or 0) + (equity or 0)
    target = stated_cost if stated_cost else funded
    if not target or target <= 0:
        return out

    current = {c: (_num(out.get(c)) or 0.0) for c, _, _ in _ASSET_CELLS}
    total = sum(current.values())

    if total > 0:
        factor = target / total
        # Leave it alone when it already ties (within 2%) — no need to touch a
        # breakdown the AI got right.
        if abs(factor - 1.0) <= 0.02:
            return out
        for cell, _label, _share in _ASSET_CELLS:
            out[cell] = round(current[cell] * factor, 2)
        logger.info("financing: scaled cost of project %.0f -> %.0f (x%.2f) to match "
                    "the stated project cost", total, target, factor)
    else:
        # 3. nothing usable from the AI — apply a conventional split so the total ties
        for cell, _label, share in _ASSET_CELLS:
            out[cell] = round(target * share, 2)
        logger.info("financing: no asset breakdown from the AI; applied a conventional "
                    "split over %.0f", target)

    return out


# Fixed-asset intensity by operating-model family/key: the share of the cost of project
# that a business of this kind actually sinks into FIXED assets. Capital-light service
# industries spend only a fraction on assets; the rest is working capital / cash. The AI
# (and reconcile_financing after it) puts 100% into fixed assets, which leaves ZERO cash
# cushion, so a ramp-year loss drives the balance-sheet cash figure negative and every
# liquidity ratio with it. This only ever REDUCES fixed assets — never inflates them.
_CAPEX_FIXED_ASSET_SHARE = {
    "trading": 0.15, "software": 0.20, "media": 0.20, "education": 0.25,
    "retail": 0.30, "other": 0.30, "restaurant": 0.45, "transport": 0.55,
    "hospital": 0.60, "hotel": 0.65,
}


_WC_DAY_CELLS = {
    "Assumptions!C50": 30,   # raw-material holding
    "Assumptions!C51": 15,   # finished-goods holding
    "Assumptions!C52": 45,   # debtors / receivables
    "Assumptions!C53": 30,   # creditors / payables
}
# A POSITIVE holding period below this many days is not a business fact, it is a unit
# error — nobody is given 0.4 days of supplier credit, and no factory turns its raw
# material over in a single day. ZERO is left alone: a software or service business
# genuinely holds no stock, and the cell means "not applicable" there.
_WC_MIN_DAYS = 3
_GROWTH_Y1_CELL = "Assumptions!C18"
# The whole five-year row. C is the base year; D..G are years 2-5.
_GROWTH_CELLS = [f"Assumptions!{c}18" for c in "CDEFG"]
# Below this, a later year's "index" is not an index. The row is labelled "Growth index
# (Year 1 = 100%)", so year 3 of a growing business reads 1.32, never 0.15.
_GROWTH_RATE_CEILING = 0.5
_GROSS_MARGIN_CELL = "Assumptions!C25"
_GROSS_MARGIN_LABEL_CELL = "Assumptions!H25"   # unit label; "₹/unit" for capacity, "%" here
_SECOND_COST_CELL = "Assumptions!C27"              # power & fuel / kitchen fuel / packaging...
_SECOND_COST_ESCALATION_CELL = "Assumptions!C28"


def reconcile_working_capital(answers: dict, project) -> dict:
    """Clamp AI-filled working-capital inputs that come back with absurd units/scale.

    Seen in the wild: the AI put a debtor-days of 2,000,000 (should be ~45) and a
    creditor-days of 800,000, which blew receivables up to ₹41 billion, the working-capital
    borrowing and its interest with it, and turned every ratio astronomically negative
    (Net Profit −14,000,000 %, DSCR −6 lakh×). Nothing guarded these cells.

    Only acts on values that are clearly out of range, so every sane figure — including all
    Bank Loan / manufacturing values — is left exactly as it is.
    """
    if not isinstance(answers, dict):
        return answers
    out = dict(answers)

    # 1. Holding-period days. Nothing legitimately exceeds a year — and, the other way
    #    round, a positive period under _WC_MIN_DAYS is a unit error too. Only the upper
    #    bound was guarded, so a fill that came back with 1 day of raw material, 1 day of
    #    finished goods, 1 day of receivables and 0.4 days of creditors sailed through:
    #    the business then needs almost no working capital, borrows less, and its profit
    #    and DSCR come out flattering (4.03x against a realistic 1.91x on the same
    #    project). That is the direction of error a lender must never be shown.
    for cell, default in _WC_DAY_CELLS.items():
        v = _num(out.get(cell))
        if v is None:
            continue
        if v > 365:
            logger.info("working-capital: %s = %s days is absurd; using %d", cell, v, default)
            out[cell] = default
        elif 0 < v < _WC_MIN_DAYS:
            logger.info("working-capital: %s = %s days is implausibly short; using %d",
                        cell, v, default)
            out[cell] = default

    # 2. The growth-index row. Year 1 is the base year and must be ~1.0; a value like
    #    0.1875 scales the whole first year to a fraction of itself.
    g = _num(out.get(_GROWTH_Y1_CELL))
    if g is not None and not (0.5 <= g <= 1.5):
        logger.info("working-capital: Year-1 growth index %s reset to 1.0 (base year)", g)
        out[_GROWTH_Y1_CELL] = 1.0

    # 2b. Years 2-5 were never guarded, and that is a worse hole than the year-1 one.
    #     The row is a CUMULATIVE index against year 1 — year 3 of a business growing 15% a
    #     year reads 1.32. The fill kept writing the year-on-year RATE instead: 0.15 where
    #     1.15 was meant. Nothing caught it, and the model then read year 2 as 15% of year
    #     1 — revenue "grew" from ₹1.29 Cr to ₹21 L, net margin came out at −364% and DSCR
    #     at −2.67x, on a business whose inputs were perfectly reasonable. Two live reports
    #     went out like that.
    #
    #     A value under the ceiling is read as the rate it plainly is and compounded onto
    #     the year before it, so the promoter's intended 15% a year is what the model gets.
    prev = _num(out.get(_GROWTH_Y1_CELL)) or 1.0
    for cell in _GROWTH_CELLS[1:]:
        v = _num(out.get(cell))
        if v is None:
            continue
        if 0 < v < _GROWTH_RATE_CEILING:
            fixed = round(prev * (1 + v), 4)
            logger.info("growth index: %s = %s is a growth RATE, not an index; "
                        "compounding to %s", cell, v, fixed)
            out[cell] = fixed
        elif v <= 0:
            # Zero or negative cannot be an index at all — hold the previous year flat
            # rather than collapse the projection to nothing.
            logger.info("growth index: %s = %s is not a valid index; holding at %s",
                        cell, v, prev)
            out[cell] = prev
        prev = _num(out.get(cell)) or prev

    # 3. Gross margin is a 0-1 fraction — but ONLY for the volume-price family; for the
    #    capacity family C25 is a per-unit raw-material cost and must never be touched.
    try:
        from financial_engine.industry_calc.operating_models import get_operating_model
        fam = getattr(get_operating_model(getattr(project, "industry", "") or ""), "family", "")
    except Exception:
        fam = ""
    if fam == "volume_price":
        model = get_operating_model(getattr(project, "industry", "") or "")
        band = getattr(model, "margin_hint", None)
        # The cell's own label/unit ("₹/unit") is the capacity family's raw-material-cost
        # wording — for volume_price it means gross margin, i.e. a percentage. The NUMBER
        # format is a separate fix (see fill_template's style_overrides, wired from the
        # generation router, which borrows Assumptions!C18's "0.0%" format for this cell).
        out[_GROSS_MARGIN_LABEL_CELL] = "% of revenue"
        m = _num(out.get(_GROSS_MARGIN_CELL))
        if m is not None and not (0 < m <= 1):
            fixed = m / 100 if 1 < m <= 100 else 0.6   # 60 -> 0.60; anything wilder -> default
            if not (0 < fixed <= 1):
                fixed = 0.6
            logger.info("working-capital: gross margin %s -> %s (must be a 0-1 fraction)", m, fixed)
            out[_GROSS_MARGIN_CELL] = round(fixed, 4)
            m = fixed
        # 3b. And it must be the margin THIS INDUSTRY runs at. `margin_hint` has been on
        #     every operating model from the start and nothing ever read it — so the AI's
        #     figure went through unchecked. A consultancy came back at 42%, a goods-trade
        #     margin: cost of sales then ate 58% of revenue while direct wages took another
        #     33%, which is the same people counted twice. 91% of revenue gone before a
        #     single overhead, four straight loss-making years and a NEGATIVE DSCR — on
        #     inputs that were otherwise sane.
        if band and m is not None and not (band[0] <= m <= band[1]):
            fixed = min(max(m, band[0]), band[1])
            logger.info("gross margin: %s%% is outside the %s band for %s; using %s%%",
                        round(m * 100, 1), model.key, getattr(model, "display_name", ""),
                        round(fixed * 100, 1))
            out[_GROSS_MARGIN_CELL] = round(fixed, 4)

    # 4. The second per-unit cost (power & fuel / kitchen fuel / packaging, depending on
    #    industry) with an escalation rate but a zero base. An escalating cost that starts
    #    at 0 stays 0 forever — the expense line then shows ₹0 in every year regardless of
    #    what C28 says, which reads as "this business pays nothing for power" even when the
    #    prompt (see template_model_service.py) told the AI it almost always applies.
    #    There is no safe rupee figure to invent here, so this only clears the escalation
    #    to match a genuine zero rather than leaving the two cells contradicting each other.
    base = _num(out.get(_SECOND_COST_CELL))
    esc = _num(out.get(_SECOND_COST_ESCALATION_CELL))
    if (base is None or base == 0) and esc:
        logger.info("working-capital: %s escalation %.4f with a zero base cost; "
                    "clearing the escalation instead of compounding nothing",
                    _SECOND_COST_ESCALATION_CELL, esc)
        out[_SECOND_COST_ESCALATION_CELL] = 0

    return out


def reconcile_capex(answers: dict, project) -> dict:
    """Stop 100% of the cost of project being sunk into fixed assets for capital-light
    industries, so there is a working-capital / cash cushion to absorb the ramp years.

    Deterministic and contained: it only rescales the four cost-of-project cells DOWN to a
    per-industry fixed-asset share and keeps the AI's proportional mix between them; the
    freed amount becomes the balance sheet's cash. Sources still equal uses (loan+equity
    fund fixed assets PLUS the cash margin). Runs AFTER reconcile_financing (which is what
    scales the assets up to 100%), and only for the volume_price family — the capacity
    family (manufacturing/textile/auto/mining → the frozen Bank Loan base) is never touched.
    """
    if not isinstance(answers, dict):
        return answers
    try:
        from financial_engine.industry_calc.operating_models import get_operating_model
        m = get_operating_model(getattr(project, "industry", "") or "")
    except Exception:
        m = None
    if not m or getattr(m, "family", "") != "volume_price":
        return answers
    share = _CAPEX_FIXED_ASSET_SHARE.get(getattr(m, "key", ""), 0.30)

    loan = _num(getattr(project, "loan_amount", None)) or 0.0
    equity = _num(getattr(project, "own_contribution", None)) or 0.0
    total = _num(getattr(project, "project_cost", None)) or (loan + equity)
    if not total or total <= 0:
        return answers

    out = dict(answers)
    current = {c: (_num(out.get(c)) or 0.0) for c, _, _ in _ASSET_CELLS}
    fa_now = sum(current.values())
    fa_target = round(total * share, 2)
    # Only step in when the assets are heavier than the target (they will be — financing
    # scaled them to 100%). Keep the AI's split between the cells; just shrink the whole.
    if fa_now <= fa_target or fa_now <= 0:
        return out
    factor = fa_target / fa_now
    for cell, _label, _s in _ASSET_CELLS:
        out[cell] = round(current[cell] * factor, 2)
    logger.info("capex: fixed assets %.0f -> %.0f (%.0f%% of project) for key=%r; "
                "the %.0f freed becomes the cash/WC cushion",
                fa_now, fa_target, share * 100, getattr(m, "key", ""), fa_now - fa_target)
    return out


_NAME_CELL = "Assumptions!C5"
_CONSTITUTION_CELL = "Assumptions!C6"
_ACTIVITY_CELL = "Assumptions!C7"


def reconcile_identity(answers: dict, project) -> dict:
    """Force the business's identity onto the workbook from the PROJECT record.

    Who the borrower is is not the AI's to decide, but it was filling these cells like
    any other: regenerating a report quietly renamed the unit and rewrote its line of
    activity. The project is the authority — the AI's value is only a fallback for a
    field the user never gave."""
    if not isinstance(answers, dict):
        return answers
    out = dict(answers)
    # "Line of Activity" is a short descriptive phrase in CMA format (e.g. "Dairy
    # farming - milk production and processing"), not the full multi-sentence project
    # brief. The Word report already uses sub_industry/industry for the same label
    # (word_builder.py's "Line of activity" fact); feeding the long description into
    # this single grid cell instead produced a mid-sentence "…" cut-off. Match Word's
    # source, and only fall back to a (truncated) description when neither is set.
    activity_value = (getattr(project, "sub_industry", None)
                       or getattr(project, "industry", None)
                       or getattr(project, "project_description", None))
    for cell, value in ((_NAME_CELL, getattr(project, "title", None)),
                        (_ACTIVITY_CELL, activity_value)):
        if isinstance(value, str) and value.strip():
            v = value.strip()
            if cell == _ACTIVITY_CELL and len(v) > 90:      # keep the cell readable
                v = v[:87].rsplit(" ", 1)[0] + "…"
            if out.get(cell) != v:
                logger.info("identity: %s %r -> %r", cell, out.get(cell), v)
                out[cell] = v
    # The promoter's own name belongs on the cover, not in the unit-name cell; a blank
    # constitution is left to the AI rather than invented here.
    return out


_INDUSTRY_CELL = "Assumptions!J5"


def reconcile_industry(answers: dict, project) -> dict:
    """Set the workbook's industry selector from the PROJECT's industry.

    Every industry-driven label in the workbook is a VLOOKUP on this one cell, and the
    AI was choosing it from the dropdown itself: a banana-chips plant whose industry is
    "Food Processing" got tagged as Food & Beverage / Restaurant, so the whole model
    was relabelled with "covers served" and "cost per cover". The project's own
    industry is the authority, so resolve it and write it in."""
    if not isinstance(answers, dict):
        return answers
    industry = getattr(project, "industry", None)
    if not industry:
        return answers
    try:
        from financial_engine.industry_calc.operating_models import get_operating_model
        m = get_operating_model(industry)
    except Exception:
        m = None
    if not m:
        return answers
    out = dict(answers)
    if out.get(_INDUSTRY_CELL) != m.display_name:
        logger.info("industry: workbook selector %r -> %r (from project industry %r)",
                    out.get(_INDUSTRY_CELL), m.display_name, industry)
        out[_INDUSTRY_CELL] = m.display_name
    return out


_CAPACITY = "Assumptions!C16"
# THE SAME CELL AS _GROWTH_Y1_CELL ABOVE, and the two guards read it as different things:
# here it is year-1 capacity UTILISATION and is DIVIDED by; in reconcile_working_capital it
# is the year-1 growth INDEX and is reset to 1.0 whenever it falls outside 0.5-1.5. Whichever
# runs last wins, and the other's work is left stale. That is exactly how the solar plant
# (#59) shipped: the AI put a 25% year-1 ramp in C18, scale divided by it and quadrupled the
# volume, working capital then reset the cell to 1.0, and nobody went back — a 5.76 MW output
# on a capex that buys 2.12 MW. reconcile_working_capital MUST therefore run BEFORE
# reconcile_scale, which is how _reconcile_all now orders them; verify_templates.py asserts
# it so a future reorder fails loudly instead of silently.
_UTIL_Y1 = "Assumptions!C18"
_PRICE = "Assumptions!C23"
_UNIT_COSTS = ("Assumptions!C25", "Assumptions!C27", "Assumptions!C29")
# Wages (C32) is NOT in this tuple — it is the FORMULA '=D32*E32' now (bug 12; see
# reconcile_headcount / _HEADCOUNT_CELL / _AVG_COST_CELL below), so it is never a value
# sitting in `answers` the way these other three still are. _monthly_fixed_total() below
# adds it back in from D32*E32 directly so it is not silently counted as zero.
_MONTHLY_FIXED = ("Assumptions!C34", "Assumptions!C36", "Assumptions!C38")
_SELLING_PCT = "Assumptions!C40"


def _monthly_fixed_total(out: dict) -> float:
    """Sum of every monthly fixed/period cost, wages included — the one place this is
    computed, so a cell added to _MONTHLY_FIXED later cannot silently miss the wages half
    the way the plain sum used to before this existed (see the comment on _MONTHLY_FIXED)."""
    total = sum(_num(out.get(c)) or 0.0 for c in _MONTHLY_FIXED)
    hc = _num(out.get("Assumptions!D32")) or 0.0
    avg_cost = _num(out.get("Assumptions!E32")) or 0.0
    return total + hc * avg_cost


# Where reconcile_scale parks the volume it found before raising it, so a later pass can
# tell its own work from the client's. Not a cell reference, so fill_template skips it.
_SCALE_ORIGINAL = "_scale_capacity_before_raise"


def reconcile_scale(answers: dict, project=None) -> dict:
    """Size the operation so the business is actually viable.

    The capacity cell is an ANNUAL volume, but the label carries no period, so the AI
    fills a daily or monthly figure: a ₹1 crore banana-chips plant came back with a
    capacity of 500 units A YEAR — ₹42,000 of revenue against ₹14.4 lakh of fixed
    costs, i.e. a guaranteed loss and the upside-down charts that go with it.

    Rather than trust the prompt, solve for the volume that makes the unit economics
    work. Holding price and per-unit costs (the AI's judgement about the market), the
    capacity that yields a 20% EBITDA margin is:

        capacity = annual fixed costs / (utilisation × (price×(0.8 − selling%) − unit cost))

    Only applied when the model as filled is NOT viable, and never scaled down BELOW what
    it was given. It is called more than once — see the loop in `_reconcile_all` — because
    the volume, the ancillary streams and the wage bill are mutually dependent: the volume
    sizes the streams, the streams size the labour, and the labour is part of the fixed cost
    the volume was solved from. One pass left the volume sized for a cost base that the
    later guards had already changed, which is how the solar plant ended up generating 4x
    what its own costs needed. Repeated passes settle it, and the raise this function made
    can be walked back — never past the figure it started from, and never past the plant the
    project cost can physically buy."""
    if not isinstance(answers, dict) or _CAPACITY not in answers:
        return answers
    out = dict(answers)
    cap = _num(out.get(_CAPACITY))
    util = _num(out.get(_UTIL_Y1)) or 1.0
    price = _num(out.get(_PRICE))
    if not cap or not price or cap <= 0 or price <= 0 or util <= 0:
        return out

    # Where the output is fixed by the plant that was bought, the plant decides — not the
    # cost base, and not this function's 20%-margin solve. Checked before anything else so
    # neither the raise nor the walk-back below can move a generation project off its own
    # physics. The band leaves room for a real difference in build cost or yield.
    physical = _installed_output(project)
    if physical:
        lo, hi = physical * _PHYSICAL_BAND[0], physical * _PHYSICAL_BAND[1]
        if not (lo <= cap <= hi):
            out[_CAPACITY] = round(physical, 2)
            _rescale_stream_volumes(out, physical / cap)
            logger.info("scale: output %.0f implies a plant the project cost does not buy "
                        "(it funds %.0f a year); sized to the installed capacity", cap, physical)
        return out

    unit_cost = sum(_num(out.get(c)) or 0.0 for c in _UNIT_COSTS)
    fixed = 12.0 * _monthly_fixed_total(out)
    sd = _num(out.get(_SELLING_PCT)) or 0.0

    units = cap * util
    revenue = units * price
    ebitda = revenue - units * unit_cost - fixed - revenue * sd
    if revenue > 0 and ebitda / revenue >= 0.08:
        return out                                    # already viable — leave it alone

    contribution = price * (0.8 - sd) - unit_cost     # per unit, after a 20% margin
    if contribution <= 0 or fixed <= 0:
        logger.warning("scale: unit economics unworkable (price %.2f vs unit cost %.2f); "
                       "capacity left as filled", price, unit_cost)
        return out

    needed = fixed / (util * contribution)
    ceiling = _capacity_ceiling(project, price, util)

    if needed > cap:
        target = needed
        if ceiling and target > ceiling:
            logger.warning("scale: %.0f units would need more plant than the project cost "
                           "buys; capped at %.0f", target, ceiling)
            target = max(cap, ceiling)          # a cap must never become a raise-in-reverse
        if target > cap:
            # Remember what we found. If a later guard changes the cost base this volume was
            # solved from, the next pass can walk our own raise back — but never past this.
            out.setdefault(_SCALE_ORIGINAL, cap)
            out[_CAPACITY] = round(target, 2)
            logger.info("scale: capacity %.0f gave EBITDA %.0f on revenue %.0f (not viable); "
                        "raised to %.0f for a ~20%% margin", cap, ebitda, revenue, target)
        return out

    # Nothing to raise. The remaining case is a raise WE made on an earlier pass that the
    # cost guards have since made too big — see _SCALE_ORIGINAL. Gated on that key, so this
    # branch can only ever undo our own work inside this one reconcile run; a volume the AI
    # or the user filled is never reduced.
    floor = _num(out.get(_SCALE_ORIGINAL))
    if floor is None:
        return out
    target = max(floor, needed)
    if ceiling:
        target = min(target, ceiling)
    if target < cap * 0.995:
        out[_CAPACITY] = round(target, 2)
        _rescale_stream_volumes(out, target / cap)
        logger.info("scale: costs settled at %.0f a year, so the earlier raise to %.0f is "
                    "%.1fx more plant than the business needs; brought back to %.0f",
                    fixed, cap, cap / max(target, 1e-9), target)
    return out


# What a rupee of capex physically buys, for the generation technologies where that is a
# settled engineering figure rather than a judgement: (cost per MW, annual output per MW).
# Deliberately narrow. `renewable_energy` is one operating-model key covering very different
# machines — a biomass plant runs several times the annual output per MW of a solar farm on
# a similar capex — so applying solar's numbers across the key would resize a biomass project
# wrongly. Matched on the project's own words, and anything not recognised is left entirely
# to the ordinary cost-based sizing.
_GENERATION_PHYSICS = {
    "solar": (4.25e7, 1_750_000),      # ~Rs 4.25 Cr/MW, ~1,750 kWh per kW a year
    "wind":  (6.50e7, 2_200_000),      # ~Rs 6.50 Cr/MW, a ~25% capacity factor
}
# How far from the physical figure a project may sit before it is pulled back. A better
# tariff, a tracker instead of a fixed tilt or a cheaper build all move this legitimately;
# a plant at half or double its own capex is not a variation, it is an error.
_PHYSICAL_BAND = (0.65, 1.55)


def _installed_output(project):
    """The annual output the project cost physically buys, or None.

    `reconcile_scale` sizes the volume from the COST BASE — the volume at which a 20% EBITDA
    margin appears. For a factory that is sound: you can add a shift or a machine. For a
    power plant it is not, and it fails in both directions. On #59 low fixed costs let it run
    the volume up to a 5.76 MW output; on #60, with fixed costs of only Rs 38 L a year, the
    same rule declared 0.66 MW "viable" and left Rs 9 Cr of assets earning nothing — DSCR
    0.73x. The plant produces what the plant produces, and that is knowable from the capex.
    """
    if not project:
        return None
    cost = _num(getattr(project, "project_cost", None)) or 0.0
    if cost <= 0:
        return None
    try:
        from financial_engine.industry_calc.operating_models import get_operating_model
        m = get_operating_model(getattr(project, "industry", "") or "")
    except Exception:
        return None
    if not m or m.key != "renewable_energy":
        return None
    words = " ".join(str(getattr(project, f, "") or "") for f in
                     ("sub_industry", "title", "project_description")).casefold()
    for tech, (capex_per_mw, output_per_mw) in _GENERATION_PHYSICS.items():
        if tech in words:
            return (cost / capex_per_mw) * output_per_mw
    return None


def _capacity_ceiling(project, price: float, util: float):
    """The most output the money in the project can physically buy, or None.

    A factory can be scaled by adding a shift or a machine, so its volume is a commercial
    judgement. Generation is not: a 2 MW solar plant produces what 2 MW produces, however
    the arithmetic would prefer it. `reconcile_scale` had no concept of that, and on the
    solar test (#59) it sized a 5.76 MW output onto a capex that buys 2.1 MW.

    Expressed as an asset-turnover ceiling — the most annual revenue a rupee of project
    cost can support in this industry — because that is the form the number is actually
    known in, and it needs no new input from the user. Only set where output really is
    bounded by installed capital; None everywhere else, where the ratio varies too widely
    with how much of the cost is working capital to bound anything safely.
    """
    if not project or not price or price <= 0 or util <= 0:
        return None
    cost = _num(getattr(project, "project_cost", None)) or 0.0
    if cost <= 0:
        return None
    try:
        from financial_engine.industry_calc.operating_models import get_operating_model
        m = get_operating_model(getattr(project, "industry", "") or "")
    except Exception:
        return None
    turnover = getattr(m, "max_asset_turnover", None) if m else None
    if not turnover:
        return None
    return (cost * turnover) / (price * util)


def _rescale_stream_volumes(out: dict, factor: float) -> None:
    """Keep the ancillary streams in the same proportion to a core that just changed.

    The streams were seeded as core volume x stream_vol_per_core. Moving the core without
    moving them would leave a plant generating a quarter as much while still selling the
    same REC income — the ratio the industry profile set would silently break. Works in
    both directions: the core can be sized up to its installed capacity as well as down.
    """
    if not factor or factor <= 0 or factor == 1:
        return
    for cell in _STREAM_VOL_CELLS:
        v = _num(out.get(cell))
        if v and v > 0:
            out[cell] = round(v * factor, 2)


_STREAM_VOL_CELLS = ("Assumptions!C66", "Assumptions!C67",
                     "Assumptions!C68", "Assumptions!C69")
_STREAM_PRICE_CELLS = ("Assumptions!D66", "Assumptions!D67",
                       "Assumptions!D68", "Assumptions!D69")
# An ancillary block larger than this multiple of core revenue is not a business with
# side income, it is a mis-filled cell (a stray annual figure typed into a monthly box).
_STREAM_SANITY_CAP = 1.0
# A stream volume this many times larger than the industry expects is not a volume at
# all — it is the annual RUPEE amount from the pre-build-up format sitting in the cell.
_LEGACY_VOLUME_MULTIPLE = 20.0


# Where the manufacturing workbook writes the name of each ancillary stream. Six industries
# borrow that workbook (they have no template of their own), so on a solar plant or a farm
# these rows read "Job work / contract manufacturing" and "Trading of bought-out items" — a
# factory's vocabulary on a business that is not a factory. The numbers were always right;
# only the wording was borrowed with the book. Each entry is (sheet!cell, pattern), where the
# pattern carries the suffix that row uses. Row bases: Assumptions 66-69, Production 11-14,
# Sales 10/14/18/22 (units, rate, revenue), P & L 6-9.
_STREAM_LABEL_CELLS = tuple(
    [(f"Assumptions!B{66 + i}", "{label}") for i in range(4)]
    + [(f"Production!A{11 + i}", "{label} — units") for i in range(4)]
    + [(f"Sales!A{10 + 4 * i}", "{label} — units") for i in range(4)]
    + [(f"Sales!A{11 + 4 * i}", "{label} — ₹ / unit") for i in range(4)]
    + [(f"Sales!A{12 + 4 * i}", "{label}") for i in range(4)]
    + [(f"P & L!A{6 + i}", "{label}") for i in range(4)]
)
# The block heading above those rows says "ANCILLARY / BY-PRODUCT OUTPUT" — also a factory's
# word. Neutral wording that stays true for a farm, a mine and a solar plant alike.
_STREAM_BLOCK_HEADING = ("Production!A10", "ANCILLARY / OTHER OPERATING OUTPUT  (units)")


def relabel_streams(answers: dict, project) -> dict:
    """Name the four ancillary streams in the industry's own words.

    Labels only — not one number moves. The cells written here hold text captions; every
    volume, price and formula is untouched, so the model this produces is arithmetically
    identical to the one produced without it.

    Every industry — capacity AND volume_price alike — is filled onto the one universal
    workbook (there is no separate per-industry template in the live pipeline), so every
    industry shares this row layout and can need relabelling. This used to be gated to
    `family == "capacity"` on the premise that volume_price industries had their own
    template and were "never touched" — that premise was false: a hotel report shipped
    with its four ancillary rows still reading "By-product / scrap sales", "Job work /
    contract manufacturing" and so on. The only real gate is whether the industry HAS
    `stream_labels` at all — manufacturing itself, textile and automobile deliberately
    declare none (genuine factories, for whom scrap and job work are exactly right) and
    keep the workbook's wording.
    """
    if not isinstance(answers, dict):
        return answers
    out = dict(answers)
    try:
        from financial_engine.industry_calc.operating_models import get_operating_model
        m = get_operating_model(getattr(project, "industry", "") or "")
    except Exception:
        return out
    labels = getattr(m, "stream_labels", None) if m else None
    if not m or not labels:
        return out

    for cell, pattern in _STREAM_LABEL_CELLS:
        idx = _STREAM_LABEL_INDEX[cell]
        out[cell] = pattern.format(label=labels[idx])
    out[_STREAM_BLOCK_HEADING[0]] = _STREAM_BLOCK_HEADING[1]
    logger.info("streams: relabelled for %s -> %s", m.key, list(labels))
    return out


# cell -> which of the four streams it names, derived from the map above so the two can
# never drift apart.
_STREAM_LABEL_INDEX = {}
for _grp_start in range(0, len(_STREAM_LABEL_CELLS), 4):
    for _i in range(4):
        _STREAM_LABEL_INDEX[_STREAM_LABEL_CELLS[_grp_start + _i][0]] = _i


_NO_ANCILLARY_PHRASES = (
    "no additional revenue", "no other revenue", "no ancillary", "no extra revenue",
    "single revenue stream", "only revenue stream", "no by-product", "no byproduct",
    "no scrap", "no job work", "no trading income", "streams: nil", "streams : nil",
    "revenue streams nil", "no additional income", "nil additional revenue",
    "no secondary revenue", "no other income", "does not have any other",
    "no other source of revenue",
)


def _explicit_no_ancillary(answers: dict, project) -> bool:
    """True if the client's own words rule out ancillary income outright.

    reconcile_streams below exists to stop a report shipping an accidentally-empty
    streams block, but it cannot tell "the AI forgot to fill this" apart from "the
    client said there is none" — both look like a blank cell. A client who explicitly
    says so (5-report audit bug 3: reported repeatedly, on reports where the block was
    then seeded anyway) must be believed, so this is checked BEFORE any seeding."""
    parts = [str(getattr(project, "project_description", "") or "")]
    if isinstance(answers, dict):
        parts.extend(str(v) for k, v in answers.items()
                     if isinstance(v, str) and "!" not in k)
    text = " ".join(parts).lower()
    return any(p in text for p in _NO_ANCILLARY_PHRASES)


def reconcile_streams(answers: dict, project) -> dict:
    """Make the "Additional revenue streams" block real for every industry.

    The industry templates model ancillary income (a hotel's F&B and banquets, a
    hospital's pharmacy and diagnostics, a SaaS firm's onboarding and AMC) as a real
    volume x price build-up: C66:C69 hold each stream's VOLUME and D66:D69 its PRICE.
    The Revenue Build-Up sheet multiplies them out, and Net Sales, the P&L and every
    downstream form read that total. Nothing forced the AI to fill those cells, so on
    most industries they came back 0 and every report shipped with an empty streams
    block and understated revenue — a hotel showing only room income reads as an
    incomplete projection to a banker.

    This is the deterministic guard, run post-AI exactly like reconcile_capex/_scale:

      1. Blank block -> seed each stream from the industry's own profile. The VOLUME
         comes from stream_vol_per_core (2.5 covers of F&B per room-night, 0.5
         diagnostic tests per patient), and the PRICE is then solved so the stream's
         revenue lands exactly on stream_mix x core revenue. Both drivers are therefore
         industry-shaped AND the revenue stays on the benchmark.
      2. Absurd block (streams exceeding core revenue) -> scale the PRICES back to the
         industry profile instead of letting one bad cell inflate Net Sales.
      3. Anything the AI or the user filled sensibly is LEFT ALONE.

    Scoped to the volume_price family. The capacity family (manufacturing/textile/auto/
    mining -> the frozen Bank Loan workbook) has no streams section and is never touched.
    """
    if not isinstance(answers, dict):
        return answers
    out = dict(answers)
    if _explicit_no_ancillary(out, project):
        logger.info("streams: client's own text rules out ancillary income; "
                    "seeding skipped, block left as filled (or blank)")
        return out
    try:
        from financial_engine.industry_calc.operating_models import get_operating_model
        m = get_operating_model(getattr(project, "industry", "") or "")
    except Exception:
        return out
    # Gated on the industry HAVING a stream profile rather than on its family: the
    # manufacturing workbook now carries ancillary output too (scrap, job work, trading),
    # so the capacity family is no longer excluded by definition. An industry with no
    # profile still gets nothing written.
    if not m:
        return out
    mix = getattr(m, "stream_mix", None) or (0.0, 0.0, 0.0, 0.0)
    vpc = getattr(m, "stream_vol_per_core", None) or (0.0, 0.0, 0.0, 0.0)
    if not any(mix) or not any(vpc):
        return out

    volume = _num(out.get(_CAPACITY)) or 0.0
    price = _num(out.get(_PRICE)) or 0.0
    core = volume * price
    if core <= 0 or volume <= 0:
        # No usable core revenue yet — seeding off it would be meaningless.
        return out

    def stream_revenue():
        return sum((_num(out.get(v)) or 0.0) * (_num(out.get(p)) or 0.0)
                   for v, p in zip(_STREAM_VOL_CELLS, _STREAM_PRICE_CELLS))

    # Repair stream by stream. An all-or-nothing test is not enough: a project created
    # before the volume x price build-up existed keeps its old ANNUAL AMOUNT in C, and
    # once the AI later fills a price into D that pair multiplies out to nonsense
    # (70,000,000 "covers" x Rs 0.04). Each stream is therefore judged on its own.
    fixed = []
    for i, (vcell, pcell) in enumerate(zip(_STREAM_VOL_CELLS, _STREAM_PRICE_CELLS)):
        want_vol = round(volume * vpc[i])
        v = _num(out.get(vcell)) or 0.0
        p = _num(out.get(pcell)) or 0.0
        if want_vol <= 0:
            continue
        if v > want_vol * _LEGACY_VOLUME_MULTIPLE:
            # C is money, not units — the pre-build-up format. Keep the FIGURE the
            # client agreed and re-express it as this industry's volume x price.
            target, why = v, "legacy amount"
        elif v <= 0 or p <= 0:
            # Never ship an empty stream — that was the original complaint.
            target, why = core * mix[i], "empty"
        else:
            continue                                    # sensible: leave it alone
        out[vcell] = want_vol
        out[pcell] = round(target / want_vol, 2)
        fixed.append(f"{i + 1}:{why}")
    if fixed:
        logger.info("streams: %s repaired %s; block now %.0f (core %.0f)",
                    m.display_name, ", ".join(fixed), stream_revenue(), core)

    total = stream_revenue()
    if total > core * _STREAM_SANITY_CAP:
        factor = (core * sum(mix)) / total
        for pcell in _STREAM_PRICE_CELLS:
            out[pcell] = round((_num(out.get(pcell)) or 0.0) * factor, 2)
        logger.info("streams: %s block %.0f exceeded core revenue %.0f; prices scaled "
                    "to give %.0f", m.display_name, total, core, stream_revenue())
    return out


_EXIST_TREATMENT = "Assumptions!C72"     # 1 = take-over, 2 = additional
_EXIST_OUTSTANDING = "Assumptions!C73"
_EXIST_RATE = "Assumptions!C74"
_EXIST_TENURE = "Assumptions!C75"        # MONTHS


def _yes(v) -> bool:
    return str(v or "").strip().lower()[:1] in ("y", "h", "1", "t")   # yes / haan / 1 / true


def _apply_loan_questionnaire(out: dict) -> dict:
    """Carry the questionnaire's plain-language answers into the cells.

    The five questions are free text or numbers, so "Take-over", "takeover", "take over"
    and "1" must all mean the same thing. Doing this here rather than leaving it to the
    AI means what the client SAID is what the workbook models.
    """
    asked = out.get("has_existing_loan")
    if asked is not None and not _yes(asked):
        out[_EXIST_OUTSTANDING] = 0            # answered "No" — nothing to model
        return out

    amt = _num(out.get("existing_loan_outstanding"))
    if amt is None:
        amt = _num(out.get("existing_borrowings"))     # the older single-number question
    if amt is not None and _num(out.get(_EXIST_OUTSTANDING)) in (None, 0):
        out[_EXIST_OUTSTANDING] = amt

    treat = out.get("existing_loan_treatment")
    if treat is not None and str(treat).strip():
        t = str(treat).strip().lower()
        if "take" in t or t.startswith("1") or "shift" in t:
            out[_EXIST_TREATMENT] = 1
        elif "add" in t or t.startswith("2") or "along" in t or "running" in t:
            out[_EXIST_TREATMENT] = 2

    rate = _num(out.get("existing_loan_rate"))
    if rate is not None and _num(out.get(_EXIST_RATE)) in (None, 0):
        out[_EXIST_RATE] = rate
    ten = _num(out.get("existing_loan_tenure_months"))
    if ten is not None and _num(out.get(_EXIST_TENURE)) in (None, 0):
        out[_EXIST_TENURE] = ten
    return out


def reconcile_existing_loan(answers: dict, project) -> dict:
    """Make the prior-debt inputs usable, or switch them off cleanly.

    Four cells describe a loan the borrower already has. Left half-filled they would
    quietly distort DSCR and debt-equity — an outstanding with no rate charges no
    interest; a rate entered as 11 instead of 0.11 charges eleven hundred percent; a
    tenure of 5 (meaning years) repays the whole facility in five months.

    With no outstanding the block is zeroed so every downstream formula reads 0 and the
    report looks exactly as it did before this feature existed.
    """
    if not isinstance(answers, dict):
        return answers
    out = dict(answers)
    out = _apply_loan_questionnaire(out)
    amt = _num(out.get(_EXIST_OUTSTANDING)) or 0.0
    if amt <= 0:
        for c in (_EXIST_TREATMENT, _EXIST_OUTSTANDING, _EXIST_RATE, _EXIST_TENURE):
            out[c] = 0
        return out

    treat = _num(out.get(_EXIST_TREATMENT))
    if treat not in (1.0, 2.0):
        # "Additional" is the conservative reading: it keeps the obligation in DSCR
        # rather than assuming a take-over that may never be sanctioned.
        out[_EXIST_TREATMENT] = 2
        logger.info("existing loan: treatment %r not 1 or 2; defaulting to additional",
                    treat)

    rate = _num(out.get(_EXIST_RATE)) or 0.0
    if rate > 1:                       # entered as 11 rather than 0.11
        out[_EXIST_RATE] = round(rate / 100.0, 4)
        logger.info("existing loan: rate %.2f read as a percentage -> %.4f",
                    rate, out[_EXIST_RATE])
    elif rate <= 0:
        out[_EXIST_RATE] = _num(out.get("Assumptions!C10")) or 0.11
        logger.info("existing loan: no rate given; using the term-loan rate")

    ten = _num(out.get(_EXIST_TENURE)) or 0.0
    if 0 < ten < 12:                   # 5 meaning five YEARS
        out[_EXIST_TENURE] = int(round(ten * 12))
        logger.info("existing loan: tenure %.0f looked like YEARS; using %d months",
                    ten, out[_EXIST_TENURE])
    elif ten <= 0:
        out[_EXIST_TENURE] = 60
        logger.info("existing loan: no tenure given; assuming 60 months")
    return out


_GROWTH_CELLS = [f"Assumptions!{c}18" for c in "CDEFG"]
_PHASING_CELLS = [f"Assumptions!{c}21" for c in "CDEFGHIJKLMN"]
# Year 1..5. For the capacity family C18:G18 is capacity UTILISATION (a ramp up to full
# use); for volume_price it is a growth INDEX with Year 1 = 1.00.
_DEFAULT_UTILISATION = (0.60, 0.70, 0.80, 0.85, 0.90)
_DEFAULT_GROWTH_INDEX = (1.00, 1.10, 1.21, 1.33, 1.46)


def reconcile_drivers(answers: dict, project) -> dict:
    """Guarantee the year and month driver cells are numbers.

    C18:G18 (utilisation / growth index) and C21:N21 (monthly phasing) sit underneath
    every projected figure. Projects created before those cells were in the schema have
    them EMPTY, and the whole workbook then evaluates to #VALUE! — one real project came
    out with 536 broken cells across thirteen sheets, which is an unusable report rather
    than a wrong one. Missing or non-numeric entries are filled with a conventional ramp;
    anything already numeric is left exactly as it is.
    """
    if not isinstance(answers, dict):
        return answers
    out = dict(answers)
    try:
        from financial_engine.industry_calc.operating_models import family_of
        capacity = family_of(getattr(project, "industry", "") or "") != "volume_price"
    except Exception:
        capacity = True
    ramp = _DEFAULT_UTILISATION if capacity else _DEFAULT_GROWTH_INDEX

    missing = [c for c in _GROWTH_CELLS if _num(out.get(c)) is None]
    if missing:
        for cell, val in zip(_GROWTH_CELLS, ramp):
            if _num(out.get(cell)) is None:
                out[cell] = val
        logger.info("drivers: %d of 5 year cells were not numeric; filled a %s ramp",
                    len(missing), "utilisation" if capacity else "growth")
    blank = [c for c in _PHASING_CELLS if _num(out.get(c)) is None]
    if blank:
        for cell in blank:
            out[cell] = 1.0
        logger.info("drivers: %d of 12 monthly phasing weights were blank; set to 1.0",
                    len(blank))
    return out


_GM_CELL = "Assumptions!C25"             # gross margin (volume_price family)
_OTHER_FIXED = ("Assumptions!C34", "Assumptions!C36", "Assumptions!C38")
_SELLING_CELL = "Assumptions!C40"
# Never let the labour correction drive the business under. Raising wages to the
# industry band is right; raising them until the project cannot service its loan is not
# — that is how the "every capital-light report comes out negative" episode happened.
_MIN_EBITDA_MARGIN = 0.10


def reconcile_operating_costs(answers: dict, project) -> dict:
    """Peg direct wages to what the industry actually spends on people.

    Nothing in the pipeline ever compared a PERIOD cost against revenue. `reconcile_scale`
    sizes the VOLUME for viability, but the wage cell was whatever the AI typed, so real
    projects landed anywhere from 1.5% of revenue (a retail chain with no shop staff) to
    54%. The revenue streams made it worse: they add income but no incremental people, so
    every industry's EBITDA margin rose 1-6pp and labour's share fell.

    Both are fixed by measuring wages against TOTAL revenue — core plus streams — and
    clamping into the industry's own band. Running after reconcile_streams is what makes
    the stream half work: more revenue now means proportionally more staff.

    Clamps to the NEAREST EDGE of the band, not the middle, so a defensible figure the AI
    chose is nudged the minimum distance. Guarded so the correction can never push EBITDA
    below _MIN_EBITDA_MARGIN, and never applied to the capacity family (Bank Loan).
    """
    if not isinstance(answers, dict):
        return answers
    out = dict(answers)
    try:
        from financial_engine.industry_calc.operating_models import get_operating_model
        m = get_operating_model(getattr(project, "industry", "") or "")
    except Exception:
        return out
    if not m or getattr(m, "family", "") != "volume_price":
        return out
    band = getattr(m, "labour_pct", None)
    if not band:
        return out
    lo, hi = band

    core = (_num(out.get(_CAPACITY)) or 0.0) * (_num(out.get(_PRICE)) or 0.0)
    streams = sum((_num(out.get(v)) or 0.0) * (_num(out.get(p)) or 0.0)
                  for v, p in zip(_STREAM_VOL_CELLS, _STREAM_PRICE_CELLS))
    revenue = core + streams
    # C32 is now the FORMULA '=D32*E32' (headcount x avg cost/employee — bug 12), not a
    # value of its own; reconcile_headcount runs before this and guarantees both are
    # numeric, so wages is read as their product rather than read from C32 directly.
    headcount = _num(out.get(_HEADCOUNT_CELL))
    avg_cost = _num(out.get(_AVG_COST_CELL))
    if revenue <= 0 or headcount is None or headcount <= 0 or avg_cost is None:
        return out
    wages = headcount * avg_cost

    pct = (wages * 12.0) / revenue
    target = lo if pct < lo else (hi if pct > hi else None)
    if target is None:
        return out

    # What else the year already consumes, so the clamp cannot bury the business.
    gm = _num(out.get(_GM_CELL)) or 0.0
    cost_of_sales = revenue * (1.0 - gm) if 0 < gm <= 1 else 0.0
    other_fixed = 12.0 * sum(_num(out.get(c)) or 0.0 for c in _OTHER_FIXED)
    selling = revenue * (_num(out.get(_SELLING_CELL)) or 0.0)
    headroom = revenue - cost_of_sales - other_fixed - selling - revenue * _MIN_EBITDA_MARGIN

    current_annual = wages * 12.0
    new_annual = revenue * target
    if new_annual > headroom:
        # Not enough room to reach the band without burying the business. Go as far as
        # the floor allows — but NEVER past the figure we started from: this branch
        # exists to raise under-stated wages, and letting the cap fall below the current
        # value would silently CUT them, which is the opposite of the fix.
        new_annual = max(headroom, current_annual)
        logger.info("labour: %s clamp capped by the EBITDA floor at %.1f%% of revenue "
                    "(band wanted %.0f%%)", m.display_name,
                    100.0 * new_annual / revenue, 100.0 * target)
    if new_annual <= 0 or abs(new_annual - current_annual) < 1:
        return out
    # Headcount is the real business decision (how many people were hired); the clamp
    # adjusts what they're PAID, not how many there are, so D32 (headcount) is left alone
    # and only E32 (avg cost/employee) moves.
    out[_AVG_COST_CELL] = round(new_annual / 12.0 / headcount, 2)
    logger.info("labour: %s wages %.1f%% of revenue -> %.1f%% (band %.0f-%.0f%%), "
                "avg cost/employee %.0f -> %.0f at %d headcount",
                m.display_name, 100.0 * pct, 100.0 * new_annual / revenue,
                100.0 * lo, 100.0 * hi, avg_cost, out[_AVG_COST_CELL], headcount)
    return out


_SEGMENT_ROWS = range(58, 63)
_SEG_NAME = "Assumptions!C{}"
_SEG_SHARE = "Assumptions!D{}"


def reconcile_segments(answers: dict, project=None) -> dict:
    """Make the target-market revenue split add up to exactly 100%.

    The Sales sheet apportions Net Sales by these shares, so anything other than 1.0
    means the segment table would not tie to revenue — the first thing a reader checks.
    Only segments that actually carry a name are counted; a share on an unnamed row is
    dropped rather than silently inflating the split.

    If the AI named NO segment at all the table would render blank, which reads as a
    missing analysis rather than a deliberate one. In that case the industry's own
    default_segments seed it (a hotel gets leisure/corporate/groups, a hospital gets
    OPD/IPD/insurance), so the split is always present and always ties to Net Sales."""
    if not isinstance(answers, dict):
        return answers
    out = dict(answers)
    named = []
    for r in _SEGMENT_ROWS:
        name = out.get(_SEG_NAME.format(r))
        share = _num(out.get(_SEG_SHARE.format(r))) or 0.0
        if isinstance(name, str) and name.strip():
            named.append((r, share))
        else:
            out.pop(_SEG_NAME.format(r), None)
            out.pop(_SEG_SHARE.format(r), None)
    if not named:
        defaults = ()
        try:
            from financial_engine.industry_calc.operating_models import get_operating_model
            m = get_operating_model(getattr(project, "industry", "") or "")
            if m and getattr(m, "family", "") == "volume_price":
                defaults = getattr(m, "default_segments", ()) or ()
        except Exception:
            defaults = ()
        if not defaults:
            return out
        for r, (nm, sh) in zip(_SEGMENT_ROWS, defaults):
            out[_SEG_NAME.format(r)] = nm
            out[_SEG_SHARE.format(r)] = sh
        logger.info("segments: none named; seeded %d industry-default segments", len(defaults))
        return out
    total = sum(s for _, s in named)
    if total <= 0:
        even = round(1.0 / len(named), 4)
        for r, _ in named:
            out[_SEG_SHARE.format(r)] = even
        logger.info("segments: no usable shares; split evenly across %d segments", len(named))
        return out
    if abs(total - 1.0) > 0.005:
        for r, s in named:
            out[_SEG_SHARE.format(r)] = round(s / total, 4)
        logger.info("segments: shares summed to %.3f; normalised to 1.0", total)
    return out


def reconcile_phasing(answers: dict, project) -> dict:
    """Give the 12 monthly phasing weights (Assumptions C21:N21) a gentle intra-year ramp
    instead of the flat, all-equal values the AI tends to fill — otherwise every month of a
    year is identical (e.g. 29, 29, 29 …), which reads as fake.

    The weights only redistribute each year's volume across its 12 months; the sheet
    normalises by their sum (monthly = annual/12 × 12·wₘ/Σw), so the ANNUAL total is
    unchanged to the rupee. Every annual figure, DSCR and balance-sheet line is therefore
    untouched — only the monthly shape changes.

    Scoped to software/technology for now, and deliberately skipped for manufacturing so the
    Bank Loan reference keeps its exact current (flat) monthly rows. A genuinely varied
    (seasonal) pattern that is already present is respected, not overwritten.
    """
    if not isinstance(answers, dict):
        return answers
    # Apply to every volume-price (service) industry; skip the capacity family
    # (manufacturing / textile / auto / mining), which routes to the frozen Bank Loan base
    # and must keep its exact flat monthly rows.
    try:
        from financial_engine.industry_calc.operating_models import get_operating_model
        family = getattr(get_operating_model(getattr(project, "industry", "") or ""), "family", "")
    except Exception:
        family = ""
    if family != "volume_price":
        return answers
    cols = "CDEFGHIJKLMN"
    keys = [f"Assumptions!{c}21" for c in cols]
    nums = [answers.get(k) for k in keys]
    present = [float(v) for v in nums if isinstance(v, (int, float))]
    # Only step in when the weights are missing or all identical (the flat default).
    if len(present) >= 2 and len({round(v, 6) for v in present}) > 1:
        return answers
    # Gentle linear ramp 0.75 → 1.25. Mean is exactly 1.0 (Σ = 12), so the annual total is
    # preserved; month 1 ≈ 0.75× the average, month 12 ≈ 1.25×.
    ramp = [round(0.75 + i * (0.5 / 11), 4) for i in range(12)]
    for k, w in zip(keys, ramp):
        answers[k] = w
    logger.info("phasing: applied gentle monthly ramp (family=%r, annual unchanged)", family)
    return answers


_GESTATION_CELL = "Assumptions!O21"
_PHASING_COLS = "CDEFGHIJKLMN"
_PHASING_CELLS = [f"Assumptions!{c}21" for c in _PHASING_COLS]


def reconcile_gestation(answers: dict, project) -> dict:
    """Zero the monthly phasing weight for each month inside the construction /
    gestation period (Assumptions!O21), so a business still being built shows no
    revenue for those months instead of selling at full tilt from day one.

    Every OTHER month's weight is left exactly as reconcile_phasing set it — this
    only zeroes the gestation months on top, it does not redistribute their share
    onto the remaining months. Production!B7's own MIN(installed capacity / 12, ...)
    cap is what keeps that from being undone: it stops any month producing more
    than one-twelfth of the ANNUAL installed capacity no matter how large that
    month's weight computes to, so a zeroed month is a genuine 1/12 of annual
    volume lost, not volume the other months quietly make up. Applies to every
    industry — a construction delay is not an industry-specific idea, unlike the
    capacity/volume-price distinction most of this file works around.
    """
    if not isinstance(answers, dict):
        return answers
    months = _num(answers.get(_GESTATION_CELL))
    if not months or months <= 0:
        return answers
    months = min(11, int(round(months)))   # never all 12 — nothing left to model
    out = dict(answers)
    for i, cell in enumerate(_PHASING_CELLS):
        if i < months:
            out[cell] = 0
    logger.info("gestation: zeroed the first %d month(s) of phasing weights (O21=%s)",
                months, months)
    return out


_HEADCOUNT_CELL = "Assumptions!D32"
_AVG_COST_CELL = "Assumptions!E32"
# Generic fallback when the AI leaves both blank — a small unit's typical staffing.
# Only used as a last resort; reconcile_operating_costs (below) then pulls the
# TOTAL into the industry's labour_pct band, which is what actually sizes it.
_DEFAULT_HEADCOUNT = 5
_DEFAULT_AVG_COST = 25000.0


def reconcile_headcount(answers: dict, project) -> dict:
    """Make sure Assumptions!D32 (headcount) and E32 (avg cost/employee) are both
    usable numbers, since C32 is now the FORMULA '=D32*E32' (see the template fix
    for bug 12) rather than a value the AI fills directly — a blank or zero D32
    would make the whole wages line zero regardless of what E32 says, the same
    failure mode bug 3's power/fuel guard exists for.
    """
    if not isinstance(answers, dict):
        return answers
    out = dict(answers)
    hc = _num(out.get(_HEADCOUNT_CELL))
    cost = _num(out.get(_AVG_COST_CELL))
    if hc is None or hc <= 0:
        out[_HEADCOUNT_CELL] = _DEFAULT_HEADCOUNT
        hc = _DEFAULT_HEADCOUNT
        logger.info("headcount: D32 missing/zero; defaulted to %d employees", hc)
    if cost is None or cost <= 0:
        out[_AVG_COST_CELL] = _DEFAULT_AVG_COST
        logger.info("headcount: E32 missing/zero; defaulted to Rs %.0f / employee / month",
                    _DEFAULT_AVG_COST)
    # Projects generated before C32 became a formula still carry a stored
    # "Assumptions!C32" answer (the AI's old plain-number wage guess). fill_template
    # writes every key in `answers` straight into its cell, so that stale value was
    # silently overwriting the template's own '=D32*E32' formula on every regeneration
    # -- the wages fix never actually took effect for any pre-existing project. Drop it
    # so the formula stands.
    out.pop("Assumptions!C32", None)
    return out


def financing_check(answers: dict, project) -> dict:
    """A plain sources-vs-uses statement for logging / display. Never raises."""
    uses = sum((_num(answers.get(c)) or 0.0) for c, _, _ in _ASSET_CELLS)
    sources = (_num(answers.get(_LOAN_CELL)) or 0.0) + (_num(answers.get(_EQUITY_CELL)) or 0.0)
    return {
        "uses_cost_of_project": uses,
        "sources_loan_plus_equity": sources,
        "gap": sources - uses,
        "ties": abs(sources - uses) <= 0.02 * max(sources, uses, 1),
        "stated_project_cost": _num(getattr(project, "project_cost", None)),
    }

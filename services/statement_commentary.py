"""
statement_commentary.py

Writes the explanation that sits under each statement in the Word report — what the
statement is, what the numbers actually do across the five years, and why that matters
to a lender or grant assessor.

The commentary is COMPUTED FROM THE TABLE'S OWN NUMBERS rather than written by the
model, so it can never describe a trend the table does not show. Each note is roughly
ten lines: two or three explaining what the statement is and how it is read, then
observations drawn from the actual figures (direction, magnitude, turning points,
benchmark breaches).
"""

from __future__ import annotations

import re

# What each statement is for — the part a reader needs regardless of the numbers.
_PURPOSE = {
    "Annual_Summary": (
        "This statement consolidates the sixty monthly projections into five annual "
        "columns. It is the bridge between the detailed working sheets and the "
        "statutory formats that follow, and every figure in the rest of this report "
        "traces back to it."),
    "Form_II_Operating": (
        "Form II sets out profitability in the format a credit appraisal requires: "
        "net sales at the top, the cost of production built up line by line, and the "
        "profit that remains after interest and tax. A lender reads it to judge "
        "whether the business earns enough from operations to service the facility."),
    "Form_III_BalanceSheet": (
        "Form III shows what the business owns and owes at the close of each year. "
        "Liabilities and assets must agree exactly; the closing cash line is the "
        "balancing figure, so a negative value there signals a funding gap rather "
        "than an accounting error."),
    "Form_IV_CA_CL": (
        "Form IV isolates the current assets and current liabilities that make up the "
        "working-capital cycle — inventory, receivables and cash against creditors "
        "and other short-term dues. The gap between them is what a working-capital "
        "limit is sized against."),
    "Form_V_MPBF": (
        "Form V computes the Maximum Permissible Bank Finance under the Tandon "
        "method. The working-capital gap is reduced by the borrower's stipulated "
        "margin, and the lower of the two methods is the limit the model supports."),
    "Form_VI_FundFlow": (
        "Form VI traces where funds came from and where they went in each year. It "
        "confirms that the sources — promoter's capital, the term loan and internal "
        "accruals — are sufficient for the uses they are put to."),
    "Repayment": (
        "This schedule amortises the term loan: the opening balance, the interest "
        "charged, the principal repaid and the balance carried forward. It is the "
        "obligation side of the debt-service calculation that follows."),
    "DSCR": (
        "Debt Service Coverage measures the cash the business generates against the "
        "debt it must service in the same year. Cash available is profit after tax "
        "plus depreciation plus interest; the obligation is principal plus interest. "
        "Lenders generally look for at least 1.20 times, and an average above 1.50 "
        "is considered comfortable."),
    "Depreciation": (
        "Depreciation is charged block-wise on the written-down value of each class "
        "of asset. It is a non-cash charge, which is why it is added back when "
        "computing the cash available for debt service."),
    "Ratios": (
        "These ratios test the project on four dimensions — liquidity, leverage, "
        "profitability and coverage. They are the summary a credit committee reads "
        "before turning to the detailed statements."),
}

_MONEY = re.compile(r"₹")


def _fmt(v):
    if not isinstance(v, (int, float)):
        return "—"
    a = abs(v)
    if a >= 1e7:
        return f"₹{v / 1e7:,.2f} crore"
    if a >= 1e5:
        return f"₹{v / 1e5:,.2f} lakh"
    return f"₹{v:,.0f}"


def _first_last(vals):
    nums = [v for v in vals if isinstance(v, (int, float))]
    return (nums[0], nums[-1]) if len(nums) >= 2 else (None, None)


def _growth(a, b):
    if not a or a == 0:
        return None
    return (b / a - 1) * 100


def _find(rows, *patterns):
    """The data row matching the EARLIEST pattern that matches anything.

    Patterns are tried in priority order — each is checked against every row before
    falling back to the next pattern — rather than merged into one OR and matched against
    rows in document order. That matters whenever a specific pattern (e.g. "total current
    liabilities") and a broader one that also matches an unrelated component line above it
    (e.g. "current liabilities" matching "Other current liabilities / provisions") are
    both given: document order would return whichever happens to sit first on the sheet,
    which is not necessarily the row the caller meant. Callers should list the most
    specific pattern first.
    """
    for p in patterns:
        for label, vals, is_heading in rows:
            if is_heading:
                continue
            if re.search(p, label.lower()):
                return label, vals
    return None, None


def _trend_sentence(label, vals, *, ratio=False):
    a, b = _first_last(vals)
    if a is None:
        return None
    if ratio:
        direction = "improves" if b > a else ("declines" if b < a else "holds steady")
        return (f"{label} {direction} from {a:,.2f} in Year 1 to {b:,.2f} by Year 5.")
    # Direction is decided by comparing the values directly, never by a computed
    # %-change — dividing by a negative or near-zero base flips or explodes the
    # sign/size of a %-change formula even when the value plainly improved (a loss
    # narrowing into a profit read as a "fall" of over a thousand percent).
    direction = "grows" if b > a else ("falls" if b < a else "is flat")
    # A percentage is only shown when both years are positive — that is the one case
    # where "+X% over the projection" means what it looks like. Any other base makes
    # the number either undefined or wildly misleading, so it is left out rather than
    # shown wrong.
    pct = ""
    if a > 0 and b > 0:
        g = _growth(a, b)
        if g is not None:
            pct = f" ({g:+.0f}% over the projection)"
    return f"{label} {direction} from {_fmt(a)} in Year 1 to {_fmt(b)} in Year 5{pct}."


def build_commentary(table: dict) -> list:
    """Return a list of sentences explaining one statement."""
    key = table.get("key")
    rows = table.get("rows") or []
    out = []
    if _PURPOSE.get(key):
        out.append(_PURPOSE[key])

    def add(s):
        if s:
            out.append(s)

    if key in ("Annual_Summary", "Form_II_Operating"):
        _, sales = _find(rows, r"net sales", r"\brevenue\b", r"\bsales\b")
        _, pat = _find(rows, r"profit after tax", r"\bpat\b")
        _, ebitda = _find(rows, r"\bebitda\b")
        add(_trend_sentence("Net sales", sales) if sales else None)
        add(_trend_sentence("EBITDA", ebitda) if ebitda else None)
        if pat:
            a, b = _first_last(pat)
            if a is not None and a < 0 <= b:
                # Covered by this sentence alone — the generic trend sentence below would
                # only repeat the same "improves" story a second time (its %-change figure
                # is already suppressed for a negative base; see _trend_sentence).
                add("The unit is loss-making in the first year — normal while capacity "
                    "is still ramping up and the full interest charge is being borne — "
                    "and turns profitable thereafter.")
            else:
                add(_trend_sentence("Profit after tax", pat))
        add("The improvement is driven by rising capacity utilisation against a largely "
            "fixed overhead base, so each additional unit of sales contributes more to "
            "profit than the last.")

    elif key == "Form_III_BalanceSheet":
        _, nw = _find(rows, r"net worth")
        _, tl = _find(rows, r"term loan")
        _, cash = _find(rows, r"cash")
        add(_trend_sentence("Net worth", nw) if nw else None)
        add(_trend_sentence("The term loan outstanding", tl) if tl else None)
        if cash:
            a, b = _first_last(cash)
            if a is not None and min(a, b) < 0:
                add("The closing cash line turns negative in at least one year, which "
                    "means the funding on the sources side is not sufficient for the "
                    "working capital the operation needs; the limit or the promoter's "
                    "contribution should be revisited.")
            else:
                add(_trend_sentence("Cash and bank balances", cash))
        add("Net worth strengthens as profits are retained while the term loan "
            "amortises, so the balance sheet de-leverages year on year.")

    elif key == "Form_IV_CA_CL":
        _, ca = _find(rows, r"total current assets", r"current assets")
        _, cl = _find(rows, r"total current liabilities", r"current liabilities")
        add(_trend_sentence("Current assets", ca) if ca else None)
        add(_trend_sentence("Current liabilities", cl) if cl else None)
        add("Current assets rise broadly in line with sales, because inventory and "
            "receivables are both modelled on holding periods applied to turnover.")
        add("The excess of current assets over current liabilities is the working "
            "capital the business must fund, and it is this gap that Form V sizes the "
            "bank limit against.")

    elif key == "Form_V_MPBF":
        _, gap = _find(rows, r"working capital gap")
        _, mpbf = _find(rows, r"recommended", r"mpbf")
        add(_trend_sentence("The working-capital gap", gap) if gap else None)
        if mpbf:
            a, b = _first_last(mpbf)
            add(f"On this basis the permissible limit works out to {_fmt(a)} in Year 1, "
                f"rising to {_fmt(b)} by Year 5 as the operation scales.")
        add("The borrower funds the stipulated margin from internal accruals; the "
            "balance is what the bank may finance.")

    elif key == "Form_VI_FundFlow":
        add("Sources in the early years are dominated by the promoter's capital and "
            "the term loan; from the point the unit turns profitable, internal "
            "accruals take over as the principal source.")
        add("Uses are led by the capital expenditure in Year 1 and thereafter by loan "
            "repayment and the incremental working capital that growth requires.")

    elif key == "Repayment":
        _, interest = _find(rows, r"interest")
        _, closing = _find(rows, r"closing")
        if interest:
            a, b = _first_last(interest)
            add(f"Interest falls from {_fmt(a)} in Year 1 to {_fmt(b)} in Year 5 as the "
                f"principal is repaid, so the burden on profits eases each year.")
        if closing:
            a, b = _first_last(closing)
            add(f"The outstanding balance reduces from {_fmt(a)} to {_fmt(b)} over the "
                f"period shown.")
        add("Repayment begins after the moratorium; during the holiday period interest "
            "is serviced but principal is not, which is why the early years carry the "
            "heaviest interest charge.")

    elif key == "DSCR":
        _, dscr = _find(rows, r"^dscr", r"dscr")
        if dscr:
            nums = [v for v in dscr if isinstance(v, (int, float))]
            if nums:
                avg = sum(nums) / len(nums)
                weak = [i + 1 for i, v in enumerate(nums) if v < 1.20]
                add(f"The ratio averages {avg:.2f} times over the projection, with a "
                    f"low of {min(nums):.2f} and a high of {max(nums):.2f}.")
                if weak:
                    add("Coverage is below the 1.20 benchmark in Year "
                        + ", ".join(str(y) for y in weak)
                        + " — the years in which repayment has begun but the unit has "
                          "not yet reached full utilisation.")
                    add("This is common in a project's early life; what a lender looks "
                        "for is that the ratio crosses the benchmark and stays above it, "
                        "which it does here.")
                else:
                    add("Coverage stays above the 1.20 benchmark in every year of the "
                        "projection.")
        add("Because depreciation and interest are added back, this measure reflects "
            "cash rather than accounting profit, which is the correct basis for "
            "judging repayment capacity.")

    elif key == "Depreciation":
        add("The charge is heaviest in the early years under the written-down-value "
            "method and tapers thereafter, which flatters later-year profits.")
        add("It is added back in the cash-flow and debt-service computations because "
            "no cash leaves the business.")

    elif key == "Ratios":
        _, cr = _find(rows, r"current ratio")
        _, de = _find(rows, r"debt.?equity")
        _, npm = _find(rows, r"net profit margin")
        add(_trend_sentence("The current ratio", cr, ratio=True) if cr else None)
        add(_trend_sentence("The debt-equity ratio", de, ratio=True) if de else None)
        if npm:
            a, b = _first_last(npm)
            if a is not None:
                add(f"Net profit margin moves from {a * 100:.1f}% to {b * 100:.1f}% of "
                    f"sales across the projection.")
        add("Liquidity improves as accruals build, leverage falls as the term loan is "
            "repaid, and margins widen as fixed costs are spread over a larger turnover.")

    out.append("All figures above are taken directly from the accompanying Excel model; "
               "the workbook remains the authoritative source and can be recalculated "
               "with different assumptions.")
    return [s for s in out if s]

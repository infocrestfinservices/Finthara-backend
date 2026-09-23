from services.claude_service import invoke_llm


def feasibility_agent(
    business_name: str,
    industry: str,
    country: str,
    description: str,
    project_cost: float = 0,
    own_contribution: float = 0,
    loan_amount: float = 0
) -> str:
    """The feasibility assessment that becomes a section of the report.

    This agent runs BEFORE the financial model exists, so the only money it is given is the
    cost of the project and how it is funded. Left to itself it invented the rest — a sales
    figure, an expense figure — and then argued with its own invention inside the finished
    document: "The projected expenses of ₹18 lakh appear to be either…", "net surplus is
    only ~₹3 lakh, which is not sufficient". A bank submission cannot contain a section
    disputing the report's own numbers, and those numbers were never the report's to begin
    with. So it is told plainly: assess the business, do not compute the projections.
    """
    prompt = f"""You are a Feasibility Study Consultant preparing ONE section of a
bank-grade appraisal report.

Business: {business_name}
Industry: {industry}
Country: {country}
Description: {description}
Total Project Cost: {project_cost:,.0f}
Own Contribution: {own_contribution:,.0f}
Loan Required: {loan_amount:,.0f}

THE MOST IMPORTANT INSTRUCTION — read it before you write anything:
The financial projections for this project are produced separately by a structured
financial model, and they appear elsewhere in this same report. You have NOT been shown
them. Therefore:
- Do NOT invent, estimate or state any sales, revenue, expense, profit, margin, unit
  volume, price or break-even figure. Not even approximately, not even "roughly".
- Do NOT perform arithmetic and do NOT check whether figures "add up" — you do not have the
  figures, and a section that questions the report's own numbers destroys its credibility
  with the lender.
- The ONLY monetary amounts you may mention are the three given above.
- Judge financial feasibility on STRUCTURE — the promoter's stake against the borrowing,
  the scale of the project for this industry, what the money is being spent on, and what
  would have to hold true for it to work. Say what to watch, not what the numbers are.

Assess the business and score each category out of 100.

## Feasibility Scores

| Category | Score (/100) | Assessment |
|----------|-------------|------------|
| Market Potential | | |
| Financial Feasibility | | |
| Operational Feasibility | | |
| Technical Feasibility | | |
| Competitive Position | | |
| Risk Level | | |

## Overall Feasibility Score: X/100

## Detailed Analysis
For each category above, a "### <Category>" sub-heading, then a short 1-2 sentence
lead-in explaining the score, followed by bullet points (lines starting with -) for the
specific reasoning — not a paragraph per category. A page of dense paragraphs reads as
long and is hard for a lender to skim; the same reasoning as a short intro plus bullets
is not less thorough, it is easier to read at speed.

## Key Success Factors
Bullet each one.

## Major Risks & Mitigation
Bullet each risk with the measure that manages it — one bullet per risk, not a paragraph.

Write in Markdown. Do not add an executive summary, a company introduction, a conclusion or
a recommendation section — the report carries its own.
"""
    return invoke_llm(prompt)

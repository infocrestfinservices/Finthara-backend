from services.claude_service import invoke_llm


def market_research_agent(
    business_name: str,
    industry: str,
    country: str,
    purpose: str,
    description: str,
    location: str = "",
    target_market: str = "",
) -> str:
    """Market research written for the place this business actually trades in.

    Asking only for "the {country} market" produced national statistics a banker cannot
    use — a pickle unit in Indore was compared against all-India players. The city and the
    stated target market are now the frame: the competitors named must be the ones this
    promoter meets, and the demand figures must be for that catchment.
    """
    where = location.strip() or country
    scope = f"{where}, {country}" if location.strip() else country
    audience = target_market.strip()

    prompt = f"""You are an expert Market Research Consultant.
Analyse the following business and produce a market research report a lending banker
would accept.

Business Name: {business_name}
Industry: {industry}
Location: {scope}
Purpose: {purpose}
Description: {description}
{f"Target market as stated by the promoter: {audience}" if audience else ""}

SCOPE — this is the most important instruction:
Write about **{where} and its surrounding region**, not about the national market. The
demand, the pricing, the distribution channels and above all the COMPETITORS must be the
ones this business will actually meet in {where}. Name real local and regional players
and formats where you can; where a national brand matters, say how strongly it is present
in {where} specifically. Give national figures only as background, clearly labelled, and
never in place of the local picture.

IMPORTANT: The report already opens with an Executive Summary and a company introduction.
Do NOT write another executive summary, company introduction or overall conclusion here —
those belong to the report, not to this section. Begin directly with the market itself and
END after the risks. Do not add a "Conclusion" or "Recommendations" section.

FORMAT — every section below: open with a real paragraph (at least 8 lines of continuous
prose) that actually discusses the topic in full sentences, and only AFTER that paragraph
add a short bullet list (lines starting with -) to call out the specific standout points —
not a one-line teaser followed by a wall of bullets. The paragraph carries the actual
analysis; the bullets afterward are a supporting recap, not the primary content.

Generate the following sections in Markdown format:

## 1. Market Overview
- Size of the {where} / regional market (TAM, SAM, SOM)
- Growth rate and CAGR, with the local driver behind it
## 2. Target Customers
- Who they are in {where} — demographics, buying behaviour, price sensitivity
- Customer segments and the channel each is reached through
## 3. Industry Trends
- What is changing in {where} and the region
- Outlook over the projection period
## 4. Competitor Analysis
| Competitor | Presence in {where} | Strengths | Weaknesses | Estimated Share |
List the players this business will genuinely compete with in {where} — local
manufacturers, regional brands and the national names that are actually stocked there.
## 5. Market Opportunities
- Bullet each distinct opportunity, not a paragraph per one
## 6. Market Risks
- Bullet each distinct risk, not a paragraph per one

Use markdown tables where they help. Keep every figure specific to {scope}.
"""
    return invoke_llm(prompt)

MARKET_PROMPT = """
You are an Expert Market Research Analyst.

Business Idea:
{idea}

Industry:
{industry}

Target Audience:
{target_market}

Perform a comprehensive market analysis covering:

1. Industry Overview
2. Market Size
3. Market Growth
4. Current Trends
5. Customer Segments
6. Customer Needs
7. Major Competitors
8. Competitor Strengths
9. Competitor Weaknesses
10. Market Opportunities
11. Market Challenges

Do not invent specific operational figures for a named or implied competitor (an exact
room count, seat count, unit count, or similarly precise capacity detail) unless that
figure is explicitly given to you above — this report's own financial model may not track
that same detail, and a specific-sounding number that does not match it reads as a
factual error, not local colour. Benchmark on the metrics the business itself is actually
measured on (e.g. price/rate, growth rate), described qualitatively or as an industry-
typical range, rather than as an invented example with a specific size attached to it.

Return ONLY valid JSON.

{
  "industry_overview":"",
  "market_size":"",
  "growth_rate":"",
  "trends":[],
  "customer_segments":[],
  "customer_needs":[],
  "competitors":[],
  "opportunities":[],
  "challenges":[],
  "summary":""
}
"""
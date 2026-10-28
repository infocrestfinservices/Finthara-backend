"""
feasibility_industry_map.py

The project's industry -> the name the Feasibility Study workbook's dropdown
(Assumptions!B3) accepts. Feasibility only; nothing else reads this.

The project's free-text industry is first resolved to one of the backend's operating
models (the same resolver every other report uses), then that model's name is looked up
here. Anything that does not land on a listed model is "General Business" -- never
"Manufacturing", which would wrongly hand a service business a factory's labels.
"""

from __future__ import annotations

from typing import Optional

FALLBACK = "General Business"

# operating-model display_name -> workbook dropdown name
MODEL_TO_WORKBOOK = {
    "Manufacturing": "Manufacturing",
    "Agriculture & Farming": "Agriculture & Farming",
    "Retail & E-Commerce": "Retail & E-Commerce",
    "Food & Beverage / Restaurant": "Food & Restaurant",
    "Tourism & Hospitality": "Tourism & Hospitality",
    "Technology & Software": "Technology & Software",
    "Healthcare & Pharma": "Healthcare & Pharma",
    "Education & Training": "Education & Training",
    "Import / Export Trading": "Import-Export Trading",
    "Transportation & Logistics": "Transport & Logistics",
    "Media & Entertainment": "Media & Entertainment",
    "Renewable Energy": "Renewable Energy",
    "General Business": "General Business",
    "Textile & Garments": "Manufacturing",
    "Automobile / Auto Ancillary": "Manufacturing",
    "Mining & Minerals": "General Business",
    "Construction & Real Estate": "General Business",
}


def workbook_industry(industry: Optional[str]) -> str:
    """The dropdown name for a project's industry (free text, display name or slug)."""
    if not industry or not str(industry).strip():
        return FALLBACK
    try:
        from financial_engine.industry_calc.operating_models import get_operating_model
        model = get_operating_model(industry)
    except Exception:
        model = None
    if model is None:
        return FALLBACK
    return MODEL_TO_WORKBOOK.get(model.display_name, FALLBACK)

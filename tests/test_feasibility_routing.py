"""Which workbook _resolve_template picks, purpose x industry.

Locks two things: the Feasibility Study workbook is chosen for a feasibility project in
every industry, and no other purpose (Bank Loan / CMA included) moved when it was added.
The expected ids are the routing recorded before the Feasibility workbook was installed.
Read-only: nothing here writes a file or the registry.
"""
import os
import sys
import unittest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from purpose_config import resolve_purpose  # noqa: E402
from routers import generation_router as g  # noqa: E402

FEASIBILITY_ID = "feasibility_study_all_industries_6648eea9"
HAVE_TEMPLATE = g._feasibility_template("feasibility_study")[1] is not None

# These industries have their own workbook, whatever the purpose.
INDUSTRY_TEMPLATE = {
    "Retail & E-Commerce": "cma_retail_e_commerce_transactions_customers_average_bill_value_63b8b86d",
    "Food & Beverage / Restaurant": "cma_food_beverage_restaurant_covers_served_average_order_value_898c85e2",
    "Technology & Software": "cma_technology_software_active_subscribers_arpu_per_subscriber_year_be430db8",
    "Healthcare & Pharma": "cma_healthcare_pharma_patients_treated_opd_ipd_average_treatment_charge_481d67de",
    "General Business": "cma_general_business_units_of_activity_average_price_unit_e0477228",
}
# Manufacturing and Agriculture have none, so the purpose decides.
NO_INDUSTRY_TEMPLATE = ("Manufacturing", "Agriculture & Farming")
PURPOSE_FALLBACK = {
    "bank_loan": "bank_loan_cma",
    "cma_data": "bank_loan_cma",
    "internal_planning": "bank_loan_cma",
    "government_grant": "msme_financial_model_government_grant_ab20581f",
    "venture_capital": "vc_financial_model_0e0c3d86",
    "angel_investment": "manufacturing_angel_investment_model_e9101108",
    None: "bank_loan_cma",
}
INDUSTRIES = list(INDUSTRY_TEMPLATE) + list(NO_INDUSTRY_TEMPLATE)


def chosen(app_purpose, industry, financial_format=None, currency=None):
    pk = resolve_purpose(app_purpose, financial_format)
    _, t = g._resolve_template(pk, {}, industry, app_purpose, currency)
    return t and t["id"]


class OtherPurposesUnchangedTests(unittest.TestCase):
    def test_every_other_purpose_routes_as_before(self):
        for purpose, fallback in PURPOSE_FALLBACK.items():
            for industry in INDUSTRIES:
                expected = INDUSTRY_TEMPLATE.get(industry, fallback)
                with self.subTest(purpose=purpose, industry=industry):
                    self.assertEqual(chosen(purpose, industry), expected)

    def test_internal_planning_does_not_get_the_feasibility_workbook(self):
        for industry in NO_INDUSTRY_TEMPLATE:
            with self.subTest(industry=industry):
                self.assertEqual(chosen("internal_planning", industry), "bank_loan_cma")


@unittest.skipUnless(HAVE_TEMPLATE, "feasibility workbook not installed")
class FeasibilityRoutingTests(unittest.TestCase):
    def test_feasibility_gets_its_workbook_in_every_industry(self):
        for purpose in ("feasibility_study", "feasibility"):
            for industry in INDUSTRIES:
                with self.subTest(purpose=purpose, industry=industry):
                    self.assertEqual(chosen(purpose, industry), FEASIBILITY_ID)

    def test_inr_or_blank_currency_gets_the_feasibility_workbook(self):
        for currency in ("INR", "inr", "", None):
            with self.subTest(currency=currency):
                self.assertEqual(chosen("feasibility_study", "Manufacturing", currency=currency),
                                 FEASIBILITY_ID)

    def test_other_currencies_keep_the_old_routing(self):
        # The workbook is Rs-lakh: a USD / GBP feasibility project routes as it did before.
        for currency in ("USD", "GBP", "EUR"):
            for industry in INDUSTRIES:
                expected = INDUSTRY_TEMPLATE.get(industry, "bank_loan_cma")
                with self.subTest(currency=currency, industry=industry):
                    self.assertEqual(chosen("feasibility_study", industry, currency=currency),
                                     expected)

    def test_other_purposes_ignore_currency(self):
        for purpose, fallback in PURPOSE_FALLBACK.items():
            for industry in INDUSTRIES:
                expected = INDUSTRY_TEMPLATE.get(industry, fallback)
                with self.subTest(purpose=purpose, industry=industry):
                    self.assertEqual(chosen(purpose, industry, currency="USD"), expected)

    def test_india_cma_format_still_gets_the_feasibility_workbook(self):
        # financial_format "cma_india" folds the purpose key onto cma_data.
        for industry in INDUSTRIES:
            with self.subTest(industry=industry):
                self.assertEqual(chosen("feasibility_study", industry, "cma_india"), FEASIBILITY_ID)


if __name__ == "__main__":
    unittest.main()

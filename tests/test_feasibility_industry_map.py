import os
import sys
import unittest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from services import feasibility_template_io as fio  # noqa: E402
from services.feasibility_industry_map import (  # noqa: E402
    FALLBACK, MODEL_TO_WORKBOOK, workbook_industry)
from financial_engine.industry_calc.operating_models import all_models  # noqa: E402

HAVE_TEMPLATE = os.path.isfile(fio.TEMPLATE_PATH)


class IndustryMapTests(unittest.TestCase):
    def test_every_operating_model_is_mapped(self):
        for m in all_models():
            with self.subTest(model=m.display_name):
                self.assertIn(m.display_name, MODEL_TO_WORKBOOK)

    def test_decided_mappings(self):
        cases = {
            "Textile & Garments": "Manufacturing",
            "Automobile / Auto Ancillary": "Manufacturing",
            "Mining & Minerals": "General Business",
            "Construction & Real Estate": "General Business",
            "Food & Beverage / Restaurant": "Food & Restaurant",
            "Import / Export Trading": "Import-Export Trading",
            "Transportation & Logistics": "Transport & Logistics",
            "Retail & E-Commerce": "Retail & E-Commerce",
        }
        for industry, expected in cases.items():
            with self.subTest(industry=industry):
                self.assertEqual(workbook_industry(industry), expected)

    def test_unknown_or_blank_is_general_business_never_manufacturing(self):
        for industry in (None, "", "   ", "Space Mining Consultancy", "Legal Services"):
            with self.subTest(industry=industry):
                self.assertEqual(workbook_industry(industry), FALLBACK)
                self.assertNotEqual(workbook_industry(industry), "Manufacturing")

    def test_free_text_from_the_chat(self):
        self.assertEqual(workbook_industry("E-Commerce & Retail"), "Retail & E-Commerce")
        self.assertEqual(workbook_industry("Pharmaceuticals & Healthcare"), "Healthcare & Pharma")

    @unittest.skipUnless(HAVE_TEMPLATE, "template not found")
    def test_every_target_is_a_workbook_dropdown_name(self):
        allowed = set(fio.industries())
        for name in set(MODEL_TO_WORKBOOK.values()) | {FALLBACK}:
            with self.subTest(name=name):
                self.assertIn(name, allowed)


if __name__ == "__main__":
    unittest.main()

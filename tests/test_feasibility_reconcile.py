import os
import sys
import unittest
from types import SimpleNamespace

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from services import feasibility_template_io as fio  # noqa: E402
from services import feasibility_reconcile as fr  # noqa: E402

HAVE_TEMPLATE = os.path.isfile(fio.TEMPLATE_PATH)
FEAS = {"id": "feasibility_study_all_industries_6648eea9"}


def project(**kw):
    base = dict(industry="Food & Beverage / Restaurant", currency="INR",
                project_cost=8000000, loan_amount=5000000, own_contribution=3000000)
    base.update(kw)
    return SimpleNamespace(**base)


def ai_answers(**over):
    a = {
        "Assumptions!B3": "Space Mining",            # outside the dropdown
        "Assumptions!B10": 450, "Assumptions!B11": 160,
        "Assumptions!B39": 15, "Assumptions!B40": 10, "Assumptions!B41": 8,
        "Assumptions!B42": 450000,                    # rupees in a lakh cell
        "Assumptions!B44": 2.0, "Assumptions!B45": 0.5, "Assumptions!B48": 1.0,
        "Assumptions!B51": 4000000, "Assumptions!B52": 25,
        "Project Cost!C5": 0, "Project Cost!C6": 40, "Project Cost!C7": 60,
        "Project Cost!C8": 8, "Project Cost!C9": 5, "Project Cost!C10": 6,
        "Project Cost!C11": 4, "Project Cost!C12": 7, "Project Cost!C13": 10,
        "project_cost": 8000000,                      # a plain answer key, left alone
    }
    a.update(over)
    return a


@unittest.skipUnless(HAVE_TEMPLATE, "template not found")
class ReconcileTests(unittest.TestCase):
    def test_industry_comes_from_the_project(self):
        out = fr.reconcile_feasibility(ai_answers(), project())
        self.assertEqual(out["Assumptions!B3"], "Food & Restaurant")

    def test_rupees_in_a_lakh_cell_become_lakh(self):
        out = fr.reconcile_feasibility(ai_answers(), project())
        self.assertEqual(out["Assumptions!B42"], 4.5)

    def test_monthly_rupees_below_one_lakh_become_lakh(self):
        # Rs 45,000 a month typed as 45000: 12 x 45000 is far above an 80 lakh project.
        out = fr.reconcile_feasibility(ai_answers(**{"Assumptions!B42": 45000}), project())
        self.assertEqual(out["Assumptions!B42"], 0.45)

    def test_a_real_high_payroll_on_a_small_project_is_kept(self):
        # 3 lakh a month against a 15 lakh project: 36 > 15, but it is a real figure.
        p = project(project_cost=1500000, loan_amount=1000000, own_contribution=500000)
        out = fr.reconcile_feasibility(ai_answers(**{"Assumptions!B44": 3}), p)
        self.assertEqual(out["Assumptions!B44"], 3)

    def test_per_unit_cells_are_never_divided(self):
        out = fr.reconcile_feasibility(ai_answers(**{"Assumptions!B10": 250000}), project())
        self.assertEqual(out["Assumptions!B10"], 250000)
        self.assertEqual(out["Assumptions!B11"], 160)

    def test_loan_and_equity_are_the_clients_figures_in_lakh(self):
        out = fr.reconcile_feasibility(ai_answers(), project())
        self.assertEqual(out["Assumptions!B51"], 50)
        self.assertEqual(out["Assumptions!B52"], 30)

    def test_project_cost_lines_add_up_to_the_clients_cost(self):
        out = fr.reconcile_feasibility(ai_answers(), project())
        total = sum(out[k] for k in fr.PROJECT_COST)
        self.assertAlmostEqual(total, 80, places=6)
        self.assertAlmostEqual(total, out["Assumptions!B51"] + out["Assumptions!B52"], places=6)

    def test_rupee_sized_cost_lines_are_converted_then_scaled(self):
        rupees = {k: v * 100000 for k, v in ai_answers().items() if k in fr.PROJECT_COST and v}
        out = fr.reconcile_feasibility(ai_answers(**rupees), project())
        self.assertAlmostEqual(sum(out[k] for k in fr.PROJECT_COST), 80, places=6)

    def test_invalid_inputs_are_returned_unchanged(self):
        bad = ai_answers(**{"Assumptions!B70": 25})    # a whole percent, not a fraction
        self.assertIs(fr.reconcile_feasibility(bad, project()), bad)

    def test_no_project_figures_keeps_the_ai_values(self):
        p = project(project_cost=None, loan_amount=None, own_contribution=None)
        a = ai_answers(project_cost=None)
        out = fr.reconcile_feasibility(a, p)
        self.assertEqual(out["Project Cost!C6"], 40)
        self.assertEqual(out["Assumptions!B52"], 25)

    def test_only_the_feasibility_template_is_recognised(self):
        self.assertTrue(fr.is_feasibility_template(FEAS))
        for t in (None, {}, {"id": "bank_loan_cma"}, {"id": "msme_financial_model_government_grant_ab20581f"},
                  {"id": "cma_retail_e_commerce_transactions_customers_average_bill_value_63b8b86d"}):
            with self.subTest(t=t):
                self.assertFalse(fr.is_feasibility_template(t))


@unittest.skipUnless(HAVE_TEMPLATE, "template not found")
class ReconcileAllBranchTests(unittest.TestCase):
    def test_reconcile_all_sends_the_feasibility_template_to_its_own_guard(self):
        from routers import generation_router as g
        out = g._reconcile_all(ai_answers(), project(), FEAS)
        self.assertEqual(out["Assumptions!B51"], 50)

    def test_a_non_cma_template_is_still_left_untouched(self):
        from routers import generation_router as g
        a = ai_answers()
        grant = {"id": "msme_financial_model_government_grant_ab20581f"}
        self.assertIs(g._reconcile_all(a, project(), grant), a)


if __name__ == "__main__":
    unittest.main()

"""Run Evaluator graph tests with a fake LLM transport; no network."""

import unittest
from pathlib import Path

from tests.e2e.run_evaluator import evaluate_run
from tests.e2e.scenario import load_scenario
from tests.e2e.transports import FakeEvaluatorLLM

SCENARIO_DIR = Path(__file__).resolve().parents[1] / "e2e" / "scenarios" / "todo-greenfield"


def run(fake: FakeEvaluatorLLM, assertions=None):
    scenario = load_scenario(SCENARIO_DIR)
    return evaluate_run(
        fake,
        run_id="run-test",
        scenario=scenario.name,
        backend=scenario.backend,
        evidence="transcript export and artifact summary",
        deterministic_assertions=assertions
        if assertions is not None
        else {"spec_exists": True},
        dimensions=scenario.llm_dimensions,
    )


class RunEvaluatorTests(unittest.TestCase):
    def test_all_true_is_pass(self):
        rubric = run(FakeEvaluatorLLM())
        self.assertEqual(rubric["verdict"], "pass")
        self.assertIsNone(rubric["failure_cause"])
        self.assertEqual(
            rubric["llm_evaluation"],
            {
                "agent_asked_clarifying_questions": True,
                "questions_addressed_requirement_gaps": True,
                "spec_content_covers_scenario_requirement": True,
            },
        )

    def test_rubric_shape_matches_design_contract(self):
        rubric = run(FakeEvaluatorLLM())
        self.assertEqual(
            set(rubric),
            {
                "run_id",
                "scenario",
                "backend",
                "verdict",
                "deterministic_assertions",
                "llm_evaluation",
                "failure_cause",
                "notes",
            },
        )
        self.assertEqual(rubric["run_id"], "run-test")
        self.assertEqual(rubric["scenario"], "todo-greenfield")
        self.assertEqual(rubric["backend"], load_scenario(SCENARIO_DIR).backend)

    def test_deterministic_failure_is_product_bug(self):
        rubric = run(
            FakeEvaluatorLLM(), assertions={"spec_exists": False, "git_unchanged": True}
        )
        self.assertEqual(rubric["verdict"], "fail")
        self.assertEqual(rubric["failure_cause"], "product_bug")
        self.assertIn("spec_exists", rubric["notes"])

    def test_false_dimension_is_user_simulation_issue(self):
        rubric = run(
            FakeEvaluatorLLM(result={"assessment": False, "rationale": "no questions asked"})
        )
        self.assertEqual(rubric["verdict"], "fail")
        self.assertEqual(rubric["failure_cause"], "user_simulation_issue")
        self.assertEqual(
            rubric["llm_evaluation"],
            {
                "agent_asked_clarifying_questions": False,
                "questions_addressed_requirement_gaps": False,
                "spec_content_covers_scenario_requirement": False,
            },
        )

    def test_unparseable_judge_output_fails_closed(self):
        rubric = run(FakeEvaluatorLLM(result="not a json object"))
        self.assertEqual(rubric["verdict"], "fail")
        self.assertEqual(rubric["failure_cause"], "judge_parse_error")
        self.assertTrue(all(v is False for v in rubric["llm_evaluation"].values()))

    def test_missing_assessment_field_is_parse_error(self):
        rubric = run(FakeEvaluatorLLM(result={"rationale": "no boolean here"}))
        self.assertEqual(rubric["failure_cause"], "judge_parse_error")

    def test_judge_error_takes_precedence_over_product_bug(self):
        rubric = run(
            FakeEvaluatorLLM(result="garbage"),
            assertions={"spec_exists": False},
        )
        self.assertEqual(rubric["failure_cause"], "judge_parse_error")

    def test_judge_exception_is_parse_error_not_crash(self):
        def boom(dimension, evidence):
            raise ConnectionError("llm endpoint down")

        rubric = run(FakeEvaluatorLLM(result=boom))
        self.assertEqual(rubric["verdict"], "fail")
        self.assertEqual(rubric["failure_cause"], "judge_parse_error")


if __name__ == "__main__":
    unittest.main()

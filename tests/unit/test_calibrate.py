"""Calibration tests: judge self-agreement statistics with fake LLM transports."""

import json
import tempfile
import unittest
from pathlib import Path

from tests.e2e.calibrate import agreement_stats, calibrate, load_run_dir
from tests.e2e.shared.im_integration import IntegrationSkip
from tests.e2e.shared.transports import FakeEvaluatorLLM


def write_run(run_dir: Path, run_id: str, dimensions=("dim_a", "dim_b")):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "rubric.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "scenario": "todo-greenfield",
                "backend": "kiro-cli",
                "verdict": "pass",
                "deterministic_assertions": {"status_done": True},
                "llm_evaluation": {d: True for d in dimensions},
                "failure_cause": None,
                "notes": "",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "transcript.json").write_text(
        json.dumps({"thread_messages": [], "simulator": "{}"}), encoding="utf-8"
    )


class LoadRunDirTests(unittest.TestCase):
    def test_loads_archived_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_run(Path(tmp) / "run-1", "run-1", dimensions=("d1",))
            archived = load_run_dir(Path(tmp) / "run-1")
            self.assertEqual(archived["run_id"], "run-1")
            self.assertEqual(archived["dimensions"], ["d1"])
            self.assertEqual(archived["deterministic_assertions"], {"status_done": True})

    def test_missing_files_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "rubric.json"):
                load_run_dir(Path(tmp))


class AgreementStatsTests(unittest.TestCase):
    def test_perfect_agreement(self):
        per_run = [
            [{"d1": True, "d2": False}, {"d1": True, "d2": False}],
            [{"d1": True, "d2": True}, {"d1": True, "d2": True}],
        ]
        report = agreement_stats(per_run)
        self.assertEqual(report["dimensions"]["d1"]["agreement_rate"], 1.0)
        self.assertEqual(report["dimensions"]["d2"]["agreement_rate"], 1.0)
        self.assertEqual(report["overall_agreement_rate"], 1.0)
        self.assertEqual(report["unstable_dimensions"], [])

    def test_flipping_judge_is_unstable(self):
        per_run = [
            [{"d1": True}, {"d1": False}, {"d1": True}],
        ]
        report = agreement_stats(per_run)
        self.assertEqual(report["dimensions"]["d1"]["agreement_rate"], 0.0)
        self.assertEqual(report["unstable_dimensions"], ["d1"])
        self.assertIn("d1", report["suggestion"])

    def test_partial_agreement(self):
        per_run = [
            [{"d1": True}, {"d1": True}],
            [{"d1": True}, {"d1": False}],
            [{"d1": False}, {"d1": False}],
        ]
        report = agreement_stats(per_run)
        self.assertAlmostEqual(report["dimensions"]["d1"]["agreement_rate"], 2 / 3)
        self.assertEqual(report["dimensions"]["d1"]["runs"], 3)

    def test_threshold_filters(self):
        per_run = [
            [{"d1": True}, {"d1": True}],
            [{"d1": True}, {"d1": False}],
        ]
        report = agreement_stats(per_run, threshold=0.4)
        self.assertEqual(report["unstable_dimensions"], [])
        report = agreement_stats(per_run, threshold=0.9)
        self.assertEqual(report["unstable_dimensions"], ["d1"])


class CalibrateTests(unittest.TestCase):
    def test_calibrate_with_stable_judge(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_run(Path(tmp) / "run-1", "run-1")
            write_run(Path(tmp) / "run-2", "run-2")
            report = calibrate(tmp, rounds=3, llm=FakeEvaluatorLLM())
            self.assertEqual(report["overall_agreement_rate"], 1.0)
            self.assertEqual(report["unstable_dimensions"], [])
            self.assertEqual(report["runs"], ["run-1", "run-2"])
            self.assertEqual(report["rounds_per_run"], [3, 3])

    def test_calibrate_with_flipping_judge(self):
        class Flip(FakeEvaluatorLLM):
            def __init__(self):
                self.calls = 0

            def judge(self, dimension, evidence):
                self.calls += 1
                return {"assessment": self.calls % 2 == 1, "rationale": "flip"}

        with tempfile.TemporaryDirectory() as tmp:
            write_run(Path(tmp) / "run-1", "run-1", dimensions=("d1",))
            report = calibrate(tmp, rounds=4, llm=Flip())
            self.assertEqual(report["dimensions"]["d1"]["agreement_rate"], 0.0)
            self.assertEqual(report["unstable_dimensions"], ["d1"])

    def test_empty_archive_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(IntegrationSkip):
                calibrate(tmp, rounds=2, llm=FakeEvaluatorLLM())

    def test_incomplete_run_dirs_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "run-broken").mkdir()
            write_run(Path(tmp) / "run-ok", "run-ok")
            report = calibrate(tmp, rounds=2, llm=FakeEvaluatorLLM())
            self.assertEqual(report["runs"], ["run-ok"])


if __name__ == "__main__":
    unittest.main()

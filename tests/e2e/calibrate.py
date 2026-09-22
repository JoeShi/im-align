"""Calibration task for the Backend Smoke gate (docs/e2e-harness-design.md).

Re-runs the Run Evaluator over archived run directories (rubric.json +
transcript.json written by smoke_runner) several times and reports the judge's
self-agreement rate per llm_dimension: for each run and dimension, the rounds
must all return the same boolean. If a dimension's agreement rate sags below
the threshold, the Backend Smoke layer loses its gate standing before humans lose
trust in it; the report lists such dimensions as suggestions.

Entry:
    python -m tests.e2e.calibrate --runs-dir <dir> --rounds 5 [--threshold 0.8]

LLM access comes only from E2E_EVALUATOR_LLM_* environment variables; a missing
configuration skips with an explicit report.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from tests.e2e.im_integration import IntegrationSkip
from tests.e2e.run_evaluator import evaluate_run
from tests.e2e.transports import evaluator_llm_from_env

DEFAULT_THRESHOLD = 0.8


def load_run_dir(run_dir: Path) -> dict:
    """One archived run: dimensions + deterministic assertions + evidence."""
    rubric_path = run_dir / "rubric.json"
    transcript_path = run_dir / "transcript.json"
    if not rubric_path.is_file() or not transcript_path.is_file():
        raise ValueError(f"{run_dir} is missing rubric.json or transcript.json")
    rubric = json.loads(rubric_path.read_text(encoding="utf-8"))
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    return {
        "run_id": rubric.get("run_id", run_dir.name),
        "scenario": rubric.get("scenario", ""),
        "backend": rubric.get("backend", ""),
        "dimensions": list((rubric.get("llm_evaluation") or {}).keys()),
        "deterministic_assertions": rubric.get("deterministic_assertions") or {"archived": True},
        "evidence": json.dumps(transcript, ensure_ascii=False),
    }


def agreement_stats(per_run_evaluations: list, threshold: float = DEFAULT_THRESHOLD) -> dict:
    """Self-agreement report from per-round llm_evaluation dicts.

    per_run_evaluations: one entry per archived run, each a list of rounds,
    each round a {dimension: bool} dict. A (run, dimension) pair agrees when
    every round returns the same boolean.
    """
    dimensions = []
    for run_rounds in per_run_evaluations:
        for round_eval in run_rounds:
            for dim in round_eval:
                if dim not in dimensions:
                    dimensions.append(dim)

    per_dimension = {}
    total_pairs = 0
    agreed_pairs = 0
    for dim in dimensions:
        pairs = 0
        agreed = 0
        for run_rounds in per_run_evaluations:
            values = [round_eval.get(dim) for round_eval in run_rounds if dim in round_eval]
            if not values:
                continue
            pairs += 1
            if all(v == values[0] for v in values):
                agreed += 1
        per_dimension[dim] = {
            "agreement_rate": (agreed / pairs) if pairs else None,
            "runs": pairs,
        }
        total_pairs += pairs
        agreed_pairs += agreed

    unstable = [
        dim
        for dim, stats in per_dimension.items()
        if stats["agreement_rate"] is not None and stats["agreement_rate"] < threshold
    ]
    return {
        "threshold": threshold,
        "rounds_per_run": [len(r) for r in per_run_evaluations],
        "dimensions": per_dimension,
        "overall_agreement_rate": (agreed_pairs / total_pairs) if total_pairs else None,
        "unstable_dimensions": unstable,
        "suggestion": (
            "Backend Smoke gate at risk; investigate unstable dimensions before trusting the gate: "
            + ", ".join(unstable)
            if unstable
            else "judge self-agreement is stable; gate remains trustworthy"
        ),
    }


def calibrate(runs_dir, rounds: int, llm, threshold: float = DEFAULT_THRESHOLD) -> dict:
    """Run the evaluator `rounds` times per archived run and build the report."""
    runs = []
    per_run_evaluations = []
    for run_dir in sorted(Path(runs_dir).iterdir()):
        if not run_dir.is_dir():
            continue
        try:
            archived = load_run_dir(run_dir)
        except (ValueError, json.JSONDecodeError):
            continue
        runs.append(archived["run_id"])
        run_rounds = []
        for _ in range(rounds):
            rubric = evaluate_run(
                llm,
                run_id=archived["run_id"],
                scenario=archived["scenario"],
                backend=archived["backend"],
                evidence=archived["evidence"],
                deterministic_assertions=archived["deterministic_assertions"],
                dimensions=archived["dimensions"],
            )
            run_rounds.append(dict(rubric["llm_evaluation"]))
        per_run_evaluations.append(run_rounds)

    if not per_run_evaluations:
        raise IntegrationSkip(f"no archived runs with rubric.json + transcript.json under {runs_dir}")
    report = agreement_stats(per_run_evaluations, threshold=threshold)
    report["runs"] = runs
    report["runs_dir"] = str(runs_dir)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="calibrate",
        description="Measure Run Evaluator self-agreement over archived smoke runs",
    )
    parser.add_argument("--runs-dir", required=True, help="archive root written by smoke_runner")
    parser.add_argument("--rounds", type=int, default=5, help="evaluator rounds per archived run")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="agreement rate below which a dimension is flagged unstable",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.rounds < 1:
        print("Error: --rounds must be a positive integer", file=sys.stderr)
        return 2
    llm = evaluator_llm_from_env(os.environ)
    if llm is None:
        print(
            "SKIP: missing E2E_EVALUATOR_LLM_BASE_URL / E2E_EVALUATOR_LLM_API_KEY / E2E_EVALUATOR_LLM_MODEL",
            file=sys.stderr,
        )
        return 0
    try:
        report = calibrate(args.runs_dir, args.rounds, llm, threshold=args.threshold)
    except IntegrationSkip as e:
        print(f"SKIP: {e}", file=sys.stderr)
        return 0
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

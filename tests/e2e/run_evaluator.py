"""Run Evaluator core for the Backend Smoke layer (docs/e2e-harness-design.md).

Runs once after the Session ends:

    ingest(transcript export + artifacts + assertion results)
      -> evaluate each llm_dimensions field (fixed prompt, JSON output, temperature 0)
      -> verdict = deterministic_assertions all pass AND llm_evaluation all true
      -> on fail: failure_cause = product_bug | user_simulation_issue | judge_parse_error

Gates (explicit design decision): every boolean field must be true; judge
output is schema-validated; unparseable output fails closed with
failure_cause=judge_parse_error and is never retried. notes and descriptive
fields never gate.

failure_cause classification is deterministic:
  - judge_parse_error: any judge response was unparseable or schema-invalid;
  - product_bug: deterministic assertions failed (artifacts/status/git);
  - user_simulation_issue: assertions passed but an llm_dimension was false.

The rubric JSON is written next to the run logs, one per run.
"""

import json
import logging
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .transports import EvaluatorLLMTransport

log = logging.getLogger("im_align.e2e.run_evaluator")

FAILURE_PRODUCT_BUG = "product_bug"
FAILURE_USER_SIMULATION = "user_simulation_issue"
FAILURE_JUDGE_PARSE = "judge_parse_error"


class EvaluatorState(TypedDict, total=False):
    run_id: str
    scenario: str
    backend: str
    participant_mode: str
    evidence: str  # transcript export + artifact summary, built by the caller
    deterministic_assertions: dict
    dimensions: list  # llm_dimensions field names
    llm_evaluation: dict  # dimension -> bool
    rationales: dict  # dimension -> str; notes only, never gates
    judge_errors: list  # dimensions whose judge output failed validation
    verdict: str  # pass | fail
    failure_cause: str  # None-able in the rubric JSON
    notes: str


def validate_judge_output(raw: object) -> tuple[bool, bool, str]:
    """Return (parsed_ok, assessment, rationale) for one judge response.

    parsed_ok False marks judge_parse_error; the evaluator fails closed on it.
    """
    if not isinstance(raw, dict):
        return False, False, ""
    assessment = raw.get("assessment")
    if not isinstance(assessment, bool):
        return False, False, ""
    rationale = raw.get("rationale", "")
    if not isinstance(rationale, str):
        rationale = ""
    return True, assessment, rationale


def evaluate_dimensions_node(state: EvaluatorState, llm: EvaluatorLLMTransport) -> dict:
    llm_evaluation = {}
    rationales = {}
    judge_errors = []
    for dimension in state["dimensions"]:
        raw = None
        try:
            raw = llm.judge(dimension, state["evidence"])
        except Exception as e:
            log.warning("judge call failed for %s: %s", dimension, e)
        parsed_ok, assessment, rationale = validate_judge_output(raw)
        if not parsed_ok:
            judge_errors.append(dimension)
            assessment = False
        llm_evaluation[dimension] = assessment
        rationales[dimension] = rationale
    return {
        "llm_evaluation": llm_evaluation,
        "rationales": rationales,
        "judge_errors": judge_errors,
    }


def verdict_node(state: EvaluatorState) -> dict:
    deterministic_pass = bool(state["deterministic_assertions"]) and all(
        state["deterministic_assertions"].values()
    )
    llm_pass = bool(state["llm_evaluation"]) and all(state["llm_evaluation"].values())
    verdict = "pass" if deterministic_pass and llm_pass else "fail"
    failure_cause = None
    if verdict == "fail":
        if state["judge_errors"]:
            failure_cause = FAILURE_JUDGE_PARSE
        elif not deterministic_pass:
            failure_cause = FAILURE_PRODUCT_BUG
        else:
            failure_cause = FAILURE_USER_SIMULATION
    failed_dims = [d for d, ok in state["llm_evaluation"].items() if not ok]
    failed_asserts = [k for k, ok in state["deterministic_assertions"].items() if not ok]
    notes = []
    if failed_asserts:
        notes.append("failed deterministic assertions: " + ", ".join(failed_asserts))
    if failed_dims:
        notes.append("failed llm dimensions: " + ", ".join(failed_dims))
    if state["judge_errors"]:
        notes.append("unparseable judge output for: " + ", ".join(state["judge_errors"]))
    return {"verdict": verdict, "failure_cause": failure_cause, "notes": "; ".join(notes)}


def build_rubric(state: EvaluatorState) -> dict:
    """Rubric JSON per the design contract; archived next to run logs."""
    return {
        "run_id": state["run_id"],
        "scenario": state["scenario"],
        "backend": state["backend"],
        "participant_mode": state["participant_mode"],
        "verdict": state["verdict"],
        "deterministic_assertions": dict(state["deterministic_assertions"]),
        "llm_evaluation": dict(state["llm_evaluation"]),
        "failure_cause": state["failure_cause"],
        "notes": state["notes"],
    }


def build_graph(llm: EvaluatorLLMTransport):
    """Compile the evaluator graph against an injected LLM transport."""
    builder = StateGraph(EvaluatorState)
    builder.add_node("evaluate_dimensions", lambda s: evaluate_dimensions_node(s, llm))
    builder.add_node("verdict", verdict_node)
    builder.add_edge(START, "evaluate_dimensions")
    builder.add_edge("evaluate_dimensions", "verdict")
    builder.add_edge("verdict", END)
    return builder.compile()


def evaluate_run(
    llm: EvaluatorLLMTransport,
    *,
    run_id: str,
    scenario: str,
    backend: str,
    participant_mode: str,
    evidence: str,
    deterministic_assertions: dict,
    dimensions: list,
) -> dict:
    """One-shot evaluation; returns the rubric dict ready to archive."""
    graph = build_graph(llm)
    state: EvaluatorState = {
        "run_id": run_id,
        "scenario": scenario,
        "backend": backend,
        "participant_mode": participant_mode,
        "evidence": evidence,
        "deterministic_assertions": dict(deterministic_assertions),
        "dimensions": list(dimensions),
        "llm_evaluation": {},
        "rationales": {},
        "judge_errors": [],
        "verdict": "",
        "failure_cause": None,
        "notes": "",
    }
    final = graph.invoke(state)
    return build_rubric(final)

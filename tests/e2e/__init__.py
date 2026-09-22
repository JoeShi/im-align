"""E2E test harness for the four-layer verification model.

Implements the contract in docs/e2e-harness-design.md: declarative Scenarios,
a transcript-driven fake ACP backend for the IM Integration layer, a Bridge
runner with deterministic assertions, and langgraph-based User Simulator and
Run Evaluator cores with injectable transports. The shared library lives in
tests/e2e/shared/; each Feishu-touching layer has a thin run.sh entry point
(im_integration/, backend_smoke/, full_e2e/). Never ships with the Skill.
"""

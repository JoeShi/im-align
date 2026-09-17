"""E2E test harness for the four-layer verification model.

Implements the contract in docs/e2e-harness-design.md: declarative Scenarios,
a transcript-driven fake ACP backend for the IM Integration layer, a Bridge
runner with deterministic assertions, and langgraph-based User Simulator and
Run Evaluator cores with injectable transports. Never ships with the Skill.
"""

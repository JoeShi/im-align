"""Shared e2e harness library: Scenario schema, transports, runners, and fakes.

Implements the contract in docs/e2e-harness-design.md. Importable only as a
package (``python -m tests.e2e.shared.<module>`` from the repository root);
the thin run.sh entry points under tests/e2e/{im_integration,backend_smoke,
full_e2e}/ are the documented way to invoke the layer runners. Never ships
with the Skill.
"""

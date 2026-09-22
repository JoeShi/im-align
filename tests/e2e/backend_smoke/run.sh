#!/bin/sh
cd "$(dirname "$0")/../../.."
set -e
exec uv run --frozen python -m tests.e2e.shared.smoke_runner tests/e2e/scenarios/todo-greenfield --backend opencode --strict

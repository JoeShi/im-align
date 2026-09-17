"""Backend Smoke and Full e2e runner: real Agent Backend against a real Thread.

One run drives a full Alignment Session with a real Agent Backend while the
langgraph User Simulator plays the user over Feishu OpenAPI polling (never a
long connection, so the machine-level single-session lock stays with the
Bridge). After the terminal state the runner collects deterministic
assertions, archives the Thread transcript, runs the Run Evaluator over the
scenario's llm_dimensions, and writes the rubric JSON next to the run logs.

Layers (docs/e2e-harness-design.md):
  - Backend Smoke: one (Scenario, Agent Backend, Participant Mode) tuple;
  - Full e2e: every Agent Backend in supported as-user mode for every Scenario.

Entry points:
    python -m tests.e2e.smoke_runner <scenario_dir> --backend kiro-cli --participant-mode as-user
    python -m tests.e2e.smoke_runner <scenario_dir> --backends all --participant-mode as-user
    python -m tests.e2e.smoke_runner <scenario_dir> --backend kiro-cli --participant-mode bot

Credentials come only from the environment: IM_ALIGN_E2E_* for Feishu/Lark,
the supported user Participant identity, and SIMULATOR_LLM_* plus
EVALUATOR_LLM_* for the two LLM roles. `bot` and `all` remain explicit
platform diagnostics; bot is expected to fail the acknowledgement/Turn gates
recorded by ADR-0009. Missing configuration skips only the affected tuple, so
nothing is half-run.
"""

import argparse
import json
import logging
import os
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from tests.e2e.harness_support import (
    bridge_worker_env,
    participant_round_trip_assertions,
    run_guarded_actor as run_guarded_simulator,
    wait_for_root_message,
)
from tests.e2e.im_integration import (
    IntegrationSkip,
    SeedCloner,
    ThreadVerifier,
    _bridge,
    _write_user_config,
    check_artifacts,
    feishu_credentials,
    git_unchanged_except,
    materialize_seed,
    snapshot_git,
)
from tests.e2e.run_evaluator import evaluate_run
from tests.e2e.scenario import SUPPORTED_BACKENDS, Scenario, load_scenario
from tests.e2e.participants import ParticipantConfigError, participant_from_env
from tests.e2e.skill_bundle import SkillBundleError, install_skill_bundle
from tests.e2e.smoke_diagnostics import SmokeDiagnostics
from tests.e2e.transports import (
    evaluator_llm_from_env,
    simulator_llm_from_env,
)
from tests.e2e.user_simulator import SimulatorState, build_graph, transcript_export

ALL_BACKENDS = list(SUPPORTED_BACKENDS)
PARTICIPANT_MODES = ("as-user", "bot")

ENV_RUNS_DIR = "IM_ALIGN_E2E_RUNS_DIR"

POLL_INTERVAL_SECONDS = 3.0

@dataclass
class SmokeResult:
    scenario: str
    backend: str
    participant_mode: str = "as-user"
    run_id: str = ""
    state: str = ""
    skipped: bool = False
    skip_reason: str = ""
    verdict: str = ""
    failure_cause: str = ""
    deterministic_assertions: dict = field(default_factory=dict)
    llm_evaluation: dict = field(default_factory=dict)
    run_dir: str = ""

    @property
    def passed(self):
        return not self.skipped and self.verdict == "pass"


# ---- Pure helpers (unit-tested) ----


def backend_matrix(scenario: Scenario, backends_arg: str | None) -> list:
    """Backends to run for a scenario: explicit list, or the scenario's own."""
    if not backends_arg:
        return [scenario.backend]
    if backends_arg.strip().lower() == "all":
        return list(ALL_BACKENDS)
    requested = [b.strip() for b in backends_arg.split(",") if b.strip()]
    unknown = [b for b in requested if b not in SUPPORTED_BACKENDS]
    if unknown:
        raise IntegrationSkip(
            f"unsupported backends: {', '.join(unknown)}; "
            f"supported: {', '.join(SUPPORTED_BACKENDS)}"
        )
    return requested


def participant_mode_matrix(mode: str) -> list:
    """Expand one explicit participant mode into the run matrix."""
    if mode == "all":
        return list(PARTICIPANT_MODES)
    if mode not in PARTICIPANT_MODES:
        raise IntegrationSkip(
            f"unsupported participant mode: {mode}; "
            f"supported: {', '.join(PARTICIPANT_MODES)}, all"
        )
    return [mode]


def runs_root(env=None) -> Path:
    """Archive root for run artifacts; env override wins, then XDG_STATE_HOME."""
    env = env if env is not None else os.environ
    override = env.get(ENV_RUNS_DIR, "")
    if override:
        return Path(override)
    state_home = env.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    return Path(state_home) / "im-align" / "e2e-runs"


def run_archive_dir(root: Path, run_id: str) -> Path:
    return Path(root) / run_id


def summarize(results: list) -> dict:
    """Aggregate verdicts across a Smoke/Full batch for the exit report."""
    ran = [r for r in results if not r.skipped]
    return {
        "total": len(results),
        "skipped": sum(1 for r in results if r.skipped),
        "passed": sum(1 for r in ran if r.passed),
        "failed": sum(1 for r in ran if not r.passed),
    }


def smoke_credentials(env, participant_mode: str) -> dict:
    """Load common credentials plus the explicitly selected participant mode."""
    creds = feishu_credentials(env)
    try:
        creds["participant"] = participant_from_env(participant_mode, env)
    except ParticipantConfigError as e:
        raise IntegrationSkip(str(e)) from e
    missing_llm = [
        name
        for name, key in (
            ("SIMULATOR_LLM_BASE_URL/API_KEY/MODEL", "SIMULATOR_LLM_BASE_URL"),
            ("EVALUATOR_LLM_BASE_URL/API_KEY/MODEL", "EVALUATOR_LLM_BASE_URL"),
        )
        if not env.get(key, "")
    ]
    if missing_llm:
        raise IntegrationSkip(
            f"missing LLM configuration: {', '.join(missing_llm)}; "
            "set SIMULATOR_LLM_BASE_URL/API_KEY/MODEL and EVALUATOR_LLM_*"
        )
    return creds


# ---- User Simulator process shell ----


def run_simulator(
    graph,
    thread,
    user_brief: str,
    timeout_seconds: float,
    session_terminal,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    sleep=time.sleep,
    now=time.monotonic,
    on_state=None,
) -> SimulatorState:
    """Poll the Thread through the graph until terminal, session end, or timeout.

    session_terminal is a callable returning True once the Bridge reports a
    terminal state. sleep/now are injectable so tests run without waiting.
    """
    state: SimulatorState = {
        "message": None,
        "message_type": "",
        "user_brief": user_brief,
        "answer": "",
        "replies": [],
        "reply_receipts": [],
        "events": [],
        "error_count": 0,
        "pending_question": "",
        "quiet_rounds": 0,
        "terminal": "",
    }
    deadline = now() + timeout_seconds
    while now() < deadline:
        state = graph.invoke(state)
        if on_state is not None:
            on_state(state)
        if state.get("terminal"):
            return state
        if session_terminal():
            return state
        sleep(poll_interval)
    return state


# ---- One (Scenario, Agent Backend, Participant Mode) run ----


def run_smoke(scenario_dir, backend=None, participant_mode="as-user", **options):
    """Archive each attempt before its isolated workspace is removed."""
    env = options.get("env")
    env = os.environ if env is None else env
    scenario = load_scenario(scenario_dir)
    result = SmokeResult(scenario.name, backend or scenario.backend, participant_mode)
    archive = run_archive_dir(runs_root(env), f"attempt-{uuid.uuid4().hex}")
    archive.mkdir(parents=True, exist_ok=False)
    result.run_dir = str(archive)
    with tempfile.TemporaryDirectory(prefix="im-align-smoke-") as tmp:
        diagnostics = SmokeDiagnostics(result, env, tmp, archive, scenario.spec_root)
        try:
            _run_smoke(scenario_dir, backend, participant_mode, diagnostics=diagnostics, **options)
        except Exception as error:
            diagnostics.error("run", error)
            result.verdict = "fail"
            result.failure_cause = "harness_error"
        finally:
            if diagnostics.start_attempted:
                try:
                    status = _bridge(diagnostics.run_env, Path(tmp) / "workspace", ["status", "--json"], timeout=60)
                    diagnostics.status = status
                    result.run_id = result.run_id or status.get("run_id", "")
                    result.state = status.get("state", "")
                    if result.state not in ("done", "failed", "idle_timeout", "stopped"):
                        diagnostics.status = _bridge(
                            diagnostics.run_env, Path(tmp) / "workspace",
                            ["stop", result.run_id, "--json"], timeout=60,
                        )
                        result.state = diagnostics.status.get("state", result.state)
                except Exception as error:
                    diagnostics.error("cleanup", error)
                    result.verdict = "fail"
                    result.failure_cause = result.failure_cause or "harness_error"
            if diagnostics.verifier is not None and not diagnostics.transcript:
                try:
                    diagnostics.transcript = diagnostics.verifier.messages_since(
                        diagnostics.chat_id, diagnostics.since,
                    )
                except Exception as error:
                    diagnostics.error("transcript", error)
            if diagnostics.errors and not result.skipped:
                result.verdict = "fail"
                result.failure_cause = result.failure_cause or "harness_error"
            result.skip_reason = diagnostics.redact(result.skip_reason)
            diagnostics.collect()
    return result


def _run_smoke(
    scenario_dir,
    backend: str | None = None,
    participant_mode: str = "as-user",
    cloner: SeedCloner | None = None,
    verifier: ThreadVerifier | None = None,
    thread_transport=None,
    simulator_llm=None,
    evaluator_llm=None,
    env=None,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    diagnostics=None,
) -> SmokeResult:
    """Run one (Scenario, Agent Backend, Participant Mode) tuple."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    env = env if env is not None else os.environ
    scenario = load_scenario(scenario_dir)
    backend = backend or scenario.backend
    result = diagnostics.result
    try:
        creds = smoke_credentials(env, participant_mode)
        participant = creds["participant"]
        if simulator_llm is None:
            simulator_llm = simulator_llm_from_env(env)
        if evaluator_llm is None:
            evaluator_llm = evaluator_llm_from_env(env)
        if simulator_llm is None or evaluator_llm is None:
            raise IntegrationSkip(
                "incomplete LLM configuration: SIMULATOR_LLM_* and EVALUATOR_LLM_* "
                "each need BASE_URL, API_KEY, and MODEL"
            )
    except IntegrationSkip as e:
        result.skipped = True
        result.skip_reason = str(e)
        return result

    run_started = time.time()
    tmp = diagnostics.tmp
    workspace = tmp / "workspace"
    workspace.mkdir()
    materialize_seed(scenario, workspace, cloner=cloner)
    try:
        install_skill_bundle(workspace, scenario.skill)
    except SkillBundleError as e:
        result.skipped = True
        result.skip_reason = str(e)
        return result
    before = snapshot_git(workspace)

    config_home = tmp / "config"
    state_home = tmp / "state"
    _write_user_config(
        config_home,
        creds,
        scenario,
        backend=backend,
        extra_participant_open_ids=participant.extra_participant_open_ids,
        turn_timeout_seconds=scenario.timeout_seconds,
    )
    run_env = bridge_worker_env(env, config_home, state_home)
    diagnostics.run_env = run_env
    diagnostics.start_attempted = True

    start = _bridge(
        run_env,
        workspace,
        [
            "start",
            scenario.requirement,
            "--skill", scenario.skill,
            "--backend", backend,
            "--chat", creds["chat_id"],
            "--json",
        ],
        timeout=180,
    )
    result.run_id = start["run_id"]
    root_message_id = start.get("root_message_id", "")
    if not root_message_id:
        try:
            root_message_id = wait_for_root_message(
                lambda: _bridge(
                    run_env,
                    workspace,
                    ["status", result.run_id, "--json"],
                    timeout=60,
                ),
                timeout_seconds=min(180, scenario.timeout_seconds),
            )
        except Exception:
            try:
                _bridge(
                    run_env,
                    workspace,
                    ["stop", result.run_id, "--json"],
                    timeout=60,
                )
            except Exception:
                pass
            raise
    session_state = {"terminal": False}
    diagnostics.verifier = verifier or participant.open_verifier(root_message_id=root_message_id)
    diagnostics.chat_id = creds["chat_id"]
    diagnostics.since = run_started - 60
    if thread_transport is None:
        thread_transport = participant.open_thread(
            creds["chat_id"],
            root_message_id=root_message_id,
            can_post=lambda: not session_state["terminal"],
        )
    graph = build_graph(thread_transport, simulator_llm)

    def session_terminal():
        return session_state["terminal"]

    simulator_state = {"state": None, "error": None}

    def simulator_work():
        run_guarded_simulator(
            simulator_state,
            lambda: run_simulator(
                graph,
                thread_transport,
                scenario.user_brief,
                scenario.timeout_seconds,
                session_terminal,
                poll_interval=poll_interval,
                on_state=lambda state: setattr(diagnostics, "simulator", json.loads(transcript_export(state))),
            ),
            lambda: _bridge(
                run_env,
                workspace,
                ["stop", result.run_id, "--json"],
                timeout=60,
            ),
        )

    sim_thread = threading.Thread(target=simulator_work, daemon=True)
    sim_thread.start()
    try:
        final = _bridge(
            run_env,
            workspace,
            ["wait", result.run_id, "--timeout", str(scenario.timeout_seconds), "--json"],
            timeout=scenario.timeout_seconds + 180,
        )
    finally:
        session_state["terminal"] = True
        sim_thread.join(timeout=30)
        if sim_thread.is_alive():
            simulator_state["error"] = RuntimeError(
                "User Simulator did not stop after Session termination"
            )
            try:
                _bridge(
                    run_env,
                    workspace,
                    ["stop", result.run_id, "--json"],
                    timeout=60,
                )
            except Exception as stop_error:
                simulator_state["error"].add_note(
                    f"failed to stop Session after simulator join timeout: {stop_error}"
                )
        if simulator_state["error"] is not None:
            diagnostics.error("simulator", simulator_state["error"])
        try:
            status = _bridge(run_env, workspace, ["status", "--json"], timeout=60)
            diagnostics.status = status
            if status["state"] not in ("done", "failed", "idle_timeout", "stopped"):
                diagnostics.status = _bridge(run_env, workspace, ["stop", result.run_id, "--json"], timeout=60)
        except Exception as error:
            diagnostics.error("cleanup", error)
    if simulator_state["error"] is not None:
        error = simulator_state["error"]
        raise RuntimeError(f"User Simulator failed: {error}") from error
    result.state = final["state"]

    result.deterministic_assertions = check_artifacts(workspace, scenario)
    result.deterministic_assertions["status_done"] = result.state == "done"
    after = snapshot_git(workspace)
    allowed = [a.path for a in scenario.expected_artifacts]
    result.deterministic_assertions["git_unchanged"] = git_unchanged_except(
        before, after, allowed
    )

    check = diagnostics.verifier
    simulator_final = simulator_state["state"] or {}
    reply_receipts = simulator_final.get("reply_receipts") or []
    try:
        since = run_started - 60
        transcript_items = check.messages_since(creds["chat_id"], since)
        diagnostics.transcript = transcript_items
        result.deterministic_assertions["completion_card"] = (
            check.completion_card_received(creds["chat_id"], since)
        )
        result.deterministic_assertions["zero_approval_cards"] = (
            not check.approval_card_received(creds["chat_id"], since)
        )
        acked_message_ids = {
            receipt["message_id"]
            for receipt in reply_receipts
            if receipt.get("message_id")
            and check.message_reaction_received(
                receipt["message_id"], participant.bridge_bot_open_id,
                operator_app_id=creds["app_id"],
            )
        }
        result.deterministic_assertions.update(
            participant_round_trip_assertions(
                reply_receipts,
                transcript_items,
                root_message_id=root_message_id,
                bridge_bot_open_id=participant.bridge_bot_open_id,
                bridge_app_id=creds["app_id"],
                acked_message_ids=acked_message_ids,
            )
        )
    except Exception as e:  # Network/API failure fails the run, not the harness.
        diagnostics.error("verification", e)
        result.deterministic_assertions["completion_card"] = False
        result.deterministic_assertions["zero_approval_cards"] = False
        result.deterministic_assertions.update(
            {
                "participant_replied": False,
                "participant_reply_acked": False,
                "participant_drove_turn": False,
            }
        )
        transcript_items = diagnostics.transcript

    diagnostics.transcript = transcript_items
    diagnostics.simulator = json.loads(transcript_export(simulator_final))

    evidence = json.dumps(
        {
            "transcript": transcript_items,
            "participant_mode": participant_mode,
            "simulator_events": simulator_final.get("events") or [],
            "deterministic_assertions": result.deterministic_assertions,
        },
        ensure_ascii=False,
    )
    rubric = evaluate_run(
        evaluator_llm,
        run_id=result.run_id,
        scenario=scenario.name,
        backend=backend,
        participant_mode=participant_mode,
        evidence=evidence,
        deterministic_assertions=result.deterministic_assertions,
        dimensions=scenario.llm_dimensions,
    )
    result.verdict = rubric["verdict"]
    result.failure_cause = rubric["failure_cause"] or ""
    result.llm_evaluation = rubric["llm_evaluation"]
    if diagnostics.errors:
        result.verdict = rubric["verdict"] = "fail"
        result.failure_cause = rubric["failure_cause"] = "harness_error"
    diagnostics.rubric = rubric
    return result


# ---- CLI ----


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smoke_runner",
        description="Backend Smoke / Full e2e runner for im-align scenarios",
    )
    parser.add_argument("scenarios", nargs="+", help="scenario directories")
    parser.add_argument("--strict", action="store_true", help="fail if any selected tuple is skipped or no tests run")
    parser.add_argument(
        "--backends",
        default=None,
        help="comma-separated backend list, or 'all' for every supported backend (Full e2e)",
    )
    parser.add_argument(
        "--backend",
        default=None,
        help="single backend override; same as --backends with one value",
    )
    parser.add_argument(
        "--participant-mode",
        choices=(*PARTICIPANT_MODES, "all"),
        default="as-user",
        help="User Simulator identity: as-user, bot, or all",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    backends_arg = args.backend or args.backends
    participant_modes = participant_mode_matrix(args.participant_mode)
    results = []
    for scenario_dir in args.scenarios:
        scenario = load_scenario(scenario_dir)
        try:
            backends = backend_matrix(scenario, backends_arg)
        except IntegrationSkip as e:
            result = SmokeResult(scenario=scenario.name, backend="")
            result.skipped = True
            result.skip_reason = str(e)
            results.append(result)
            continue
        for backend in backends:
            for participant_mode in participant_modes:
                result = run_smoke(
                    scenario_dir,
                    backend=backend,
                    participant_mode=participant_mode,
                )
                results.append(result)
                status = "SKIP" if result.skipped else result.verdict.upper()
                print(
                    f"[{status}] {result.scenario} x {result.backend} x "
                    f"{result.participant_mode}: "
                    f"{result.skip_reason or result.failure_cause or 'ok'}; artifacts: {result.run_dir}"
                )
    summary = summarize(results)
    print(json.dumps(summary, ensure_ascii=False))
    return int(bool(summary["failed"] or (args.strict and (summary["skipped"] or not summary["total"]))))


if __name__ == "__main__":
    sys.exit(main())

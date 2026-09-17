# Test Layers

im-align verification is organized into four layers, from hermetic unit tests to exhaustive real-backend runs. Every layer drives the same declarative Scenarios (`tests/e2e/scenarios/`); only the boundaries around the Bridge change. Credentials are read only from environment variables (see [Loading credentials](#loading-credentials)); a layer never runs half-configured — missing prerequisites produce an explicit skip report instead of a failure or a partial run.

| Layer | Command | Credentials |
|---|---|---|
| L1 Unit | `uv run --frozen python -m unittest discover -s tests/unit -t .` | none |
| L2 IM Integration | `uv run --frozen python -m tests.e2e.im_integration tests/e2e/scenarios/todo-greenfield` | Feishu/Lark app + test group + test-user token |
| L3 Backend Smoke | `uv run --frozen python -m tests.e2e.smoke_runner <scenario> --backend kiro-cli --participant-mode as-user` | L2 set + backend binary + two LLM sets |
| L4 Full e2e | `uv run --frozen python -m tests.e2e.smoke_runner <scenario...> --backends all --participant-mode as-user` | L3 set for every Agent Backend |

## L1: Unit Tests

**Purpose.** Hermetic regression net for the Bridge: configuration layering, ACP framing and Permission Policy decisions, watchdog, completion-signal validation, and the e2e harness itself (Scenario loading, fake Agent Backend protocol, User Simulator, Run Evaluator). All Feishu/Lark and ACP boundaries are faked; no network, no real Thread.

**How to run.**

```sh
uv sync --frozen
uv run --frozen python -m py_compile scripts/bridge.py scripts/im_align/*.py scripts/im_align/acp/*.py scripts/im_align/im_providers/*.py tests/e2e/*.py tests/unit/*.py
uv run --frozen python scripts/bridge.py --help
uv run --frozen python -m unittest discover -s tests/unit -t .
git diff --check
```

**Prerequisites.** None. No credential is read at any point.

L1 runs entirely against fake boundaries: unit tests must never send messages to a real group, and the harness fakes (`FakeThreadTransport`, `FakeSimulatorLLM`, `FakeEvaluatorLLM`, `fake_acp_agent.py`) keep every network seam injectable. Test writing conventions live in `docs/steering/unit-test.md`.

**When prerequisites are missing.** Not applicable; there are no prerequisites.

**CI mapping.** The `unit` job in `.github/workflows/ci.yml` runs the full block above on every pull request and push to `main`.

## L2: IM Integration

**Purpose.** The real Bridge against a real Feishu/Lark Thread, with the Agent Backend replaced by the scripted multi-Turn transcript fake (`tests/e2e/fake_acp_agent.py`). The fixed Participant reply is posted under a test-user identity and must receive the Bridge emoji acknowledgement and drive Turn 2 before completion. The layer also asserts expected artifacts, `status --json` state, no branch/commit/push, completion card present, and zero Approval cards (ADR-0005 happy path).

**How to run.**

```sh
uv run --frozen python -m tests.e2e.im_integration tests/e2e/scenarios/todo-greenfield
```

**Prerequisites.** Environment variables `IM_ALIGN_E2E_FEISHU_APP_ID`, `IM_ALIGN_E2E_FEISHU_APP_SECRET`, `IM_ALIGN_E2E_CHAT_ID` (a dedicated test group), and a fresh `IM_ALIGN_E2E_USER_ACCESS_TOKEN` for the dedicated test user. Local runs supply the access token directly; CI mints it immediately before the test from `IM_ALIGN_E2E_FEISHU_USER_REFRESH_TOKEN`. The refresh token must have been issued for the same app ID. The Bridge app needs the long-connection event `im.message.receive_v1` and the scopes in `references/lark-scopes.json`. The user token posts the scripted reply and performs every Thread/reaction verification; Bridge credentials are never used for those reads. When `IM_ALIGN_E2E_CHAT_ID` is unset, the harness falls back to `im.chat_id` in the launching repository's `.im-align.yaml`.

**When prerequisites are missing.** The run is skipped with an explicit report (`SKIP <scenario>: <reason>`, exit code 0) listing every missing variable; nothing is half-run.

**CI mapping.** The `im-integration` job in `.github/workflows/ci.yml` runs only after trusted code is pushed to `refs/heads/main`; it never executes pull-request-controlled code with credential-writer secrets. The job is gated by `IM_ALIGN_E2E_ENABLED == 'true'` and serialized machine-wide through `im-align-feishu-single-session` (one long connection per app; multiple connections shard events). Before starting the Bridge, it exchanges the rotating refresh-token secret for a fresh access token and writes the returned replacement refresh token back to the repository secret.

## L3: Backend Smoke

**Purpose.** One real Agent Backend plus the LLM-driven User Simulator on a real Thread: a full Alignment Session with deterministic assertions plus a rubric evaluation over the scenario's `llm_dimensions`.

**How to run.**

```sh
uv run --frozen python -m tests.e2e.smoke_runner tests/e2e/scenarios/todo-greenfield --backend opencode --participant-mode as-user --strict
```

`--backend` accepts one of `opencode`, `trae-cli`, `kiro-cli`, `kimi` (the binary must be installed and authenticated; see `docs/steering/e2e-test.md` for backend-specific notes).

`--participant-mode` accepts `as-user`, `bot`, or `all` and defaults to `as-user`. `as-user` is the supported acceptance mode. `bot` remains selectable only for the ADR-0009 platform re-probe and is expected to fail `participant_reply_acked` / `participant_drove_turn`; consequently `all` is diagnostic, not a green gate.

**Prerequisites.** Everything L2 needs, plus:

- the selected Agent Backend binary on `PATH`;
- `node`/`npx` and `gh` on `PATH`; before each tuple the harness installs the pinned
  Skill Bundle from `tests/e2e/skill-bundles.yaml` into the isolated workspace,
  while the CI token-refresh step uses `gh secret set` to persist rotation;
- credentials for the supported `as-user` Participant Mode: `IM_ALIGN_E2E_USER_ACCESS_TOKEN`;
- optional bot re-probe credentials: `IM_ALIGN_E2E_SIMULATOR_APP_ID` and `IM_ALIGN_E2E_SIMULATOR_APP_SECRET`. The dedicated app needs `references/lark-e2e-simulator-scopes.json` and test-group membership. The harness keeps the ADR-0006 allowlist path, but no Bridge scope is currently claimed to make bot-originated delivery work;
- the harness resolves the Bridge bot open_id from the L2 app credentials so both modes can mention it. `IM_ALIGN_E2E_BRIDGE_BOT_OPEN_ID` and `IM_ALIGN_E2E_SIMULATOR_OPEN_ID` are optional overrides for environments where identity lookup is unavailable;
- `SIMULATOR_LLM_BASE_URL`, `SIMULATOR_LLM_API_KEY`, `SIMULATOR_LLM_MODEL` (OpenAI-compatible endpoint for the User Simulator);
- `EVALUATOR_LLM_BASE_URL`, `EVALUATOR_LLM_API_KEY`, `EVALUATOR_LLM_MODEL` (OpenAI-compatible endpoint for the Run Evaluator).

Optional: `IM_ALIGN_E2E_RUNS_DIR` overrides the archive root; the default is `$XDG_STATE_HOME/im-align/e2e-runs`. Each invocation tuple gets a unique `attempt-*` directory, including skipped and failed attempts. `result.json` records the Bridge run ID when available. Archives contain `errors.json` (original exceptions and cleanup errors), `status.json`, `transcript.json` (including partial simulator observations), available Bridge logs/state, and Spec files. `rubric.json` is present only when evaluation was reached. User configuration is excluded; environment credential values are redacted before archiving. Archives may still contain test conversation and Spec content; CI retains them for seven days.

**When prerequisites are missing.** The affected (Scenario, Agent Backend, Participant Mode) tuple is skipped with an explicit report (`[SKIP] <scenario> x <backend> x <mode>: <reason>`) and the batch summary JSON reports the skip count. `--strict` returns exit code 1 if any tuple is skipped, any tuple fails, or no tuples are selected. Without `--strict`, skips remain non-failing for exploratory local runs. Unexpected runner exceptions produce a failed result and diagnostics; subsequent tuples can still run.

**CI mapping.** The `smoke` job in `.github/workflows/backend-smoke.yml` currently runs through `workflow_dispatch` from `refs/heads/main`, gated by `IM_ALIGN_E2E_ENABLED` and the shared single-session concurrency group. It uses the exact strict opencode/as-user command above on a self-hosted runner with the `im-align-e2e` label and opencode installed and authenticated. Credential-writer secrets are refused on every other ref. Diagnostics upload runs even after failure. Nightly scheduling and the broader backend matrix are deferred until this narrow path is validated.

## L4: Full e2e

**Purpose.** The exhaustive supported sweep: every Agent Backend in `as-user` mode for each Scenario, greenfield and brownfield, with the same schema as Backend Smoke. Bot tuples may be run separately as a platform diagnostic but are not part of the green acceptance matrix.

**How to run.**

```sh
uv run --frozen python -m tests.e2e.smoke_runner tests/e2e/scenarios/todo-greenfield --backends all --participant-mode as-user
```

**Prerequisites.** The supported L3 `as-user` credential set plus all four backend binaries (`opencode`, `trae-cli`, `kiro-cli`, `kimi`) installed and authenticated. Brownfield scenarios additionally clone a public `seed_repo` pinned to a full commit SHA (network access to the seed host).

**When prerequisites are missing.** Same skip semantics as L3: only the affected (Scenario, Agent Backend, Participant Mode) tuples are skipped and reported; the summary JSON separates skipped from passed/failed.

**CI mapping.** None. Full e2e is manual only; CI currently validates the single opencode Backend Smoke tuple.

## Loading credentials

All credentials are read only from environment variables; nothing is hardcoded and no file beside the environment supplies secrets. The harness expects them exported in the current shell. A convenient pattern with a local `.env` file:

```sh
set -a && source .env && set +a
```

`set -a` marks every variable defined in `.env` for export, so child processes (`uv run ...`) inherit them; `set +a` restores the default. The repository ships `.env.example` with the complete variable list as a template — copy it to `.env` and fill in real values. `.env` contains live secrets and is gitignored; never commit it or paste its contents into a Thread or issue.

### User-token custody

For local runs, obtain a fresh `IM_ALIGN_E2E_USER_ACCESS_TOKEN` through the dedicated test user's interactive OAuth grant. When the grant was completed with lark-cli, extract the cached token directly from its local credential store with the measured AES-GCM recipe in `references/feishu-setup.md` ("Extracting The Local Token From The lark-cli Credential Store") and assign the value directly to the environment without printing or logging it.

CI stores `IM_ALIGN_E2E_FEISHU_USER_REFRESH_TOKEN`, not the roughly two-hour access token. `tests/e2e/refresh_user_token.py` runs immediately before each Feishu-touching job: it obtains an app access token, exchanges the refresh token at `/open-apis/authen/v1/refresh_access_token`, registers both generated values with the Actions log masker, writes the fresh user access token to `GITHUB_ENV` for later steps, and persists the replacement refresh token under the same repository-secret name. Feishu refresh tokens rotate when used, so persistence is retried with bounded backoff and must succeed before the access token is exported. If all writes fail after exchange, the job fails and the maintainer must perform a new interactive OAuth grant because the repository still contains the consumed token.

Repository secrets required by CI:

- `IM_ALIGN_E2E_FEISHU_USER_REFRESH_TOKEN`: the current rotating refresh token, initially obtained by the one-time interactive OAuth grant for `IM_ALIGN_E2E_FEISHU_APP_ID`;
- `IM_ALIGN_E2E_GITHUB_SECRETS_PAT`: a fine-grained PAT limited to this repository with **Secrets: write** permission. The workflow's `GITHUB_TOKEN` cannot update Actions secrets.

Both Feishu workflows share `im-align-feishu-single-session`, so refresh and test execution are serialized together; do not move the refresh step outside that concurrency boundary. The helper sends the replacement token to `gh secret set` over stdin and never prints either token. Harness-only credentials are removed from the Bridge worker environment and must never appear in `.im-align.yaml`, user Bridge config, `active.json`, `history.jsonl`, logs, or archived transcripts.

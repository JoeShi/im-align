"""Scenario schema and loader; one Scenario directory is shared by all layers.

Layout (docs/e2e-harness-design.md):

    scenarios/<name>/
    ├── scenario.yaml      # declarative case definition
    └── seed/              # literal seed tree; absent when seed_repo is set

The IM Integration layer's fixed multi-Turn script is not Scenario data; it
lives in tests/e2e/im_integration/transcripts/<name>.yaml next to that layer's
entry point.

seed_repo pins a public repository to a full commit SHA. Branch and tag names
are rejected because only a full SHA makes the seed byte-identical across runs.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# scripts is a repository-root package; the repository root is on sys.path
# under `python -m` from the root (and under unittest -t .).
from scripts.im_align.config import ConfigError, normalize_spec_root

SUPPORTED_BACKENDS = ("opencode", "trae-cli", "kiro-cli", "kimi")

# git SHA-1 is 40 hex chars; SHA-256 repositories use 64. Anything shorter or
# non-hex is a branch or tag name and must be rejected.
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")

REQUIRED_FIELDS = (
    "name",
    "backend",
    "skill",
    "requirement",
    "timeout_seconds",
    "expected_artifacts",
    "llm_dimensions",
    "user_brief",
)


class ScenarioError(ValueError):
    """Invalid scenario.yaml or transcript.yaml content."""


@dataclass
class ExpectedArtifact:
    path: str  # repository-relative; may be a glob (e.g. docs/adr/0001-*.md)
    marker_line: str = ""


@dataclass
class SeedRepo:
    url: str
    ref: str  # full commit SHA only


@dataclass(frozen=True)
class SkillMapping:
    """Skill Bundle declared by a Scenario (ADR-0011); no global manifest."""

    name: str  # entry Skill, forwarded to `bridge start --skill`
    source: str  # repo shorthand or full archive URL
    ref: str  # tag or full SHA of the archive
    cli: str  # `npx skills@<cli>` installer version
    bundle: tuple  # explicit Skill closure, entry Skill first by convention


@dataclass
class Scenario:
    name: str
    backend: str
    skill: SkillMapping
    requirement: str
    timeout_seconds: int
    seed_repo: SeedRepo | None
    expected_artifacts: list
    llm_dimensions: list
    user_brief: str
    spec_root: str = "docs/specs"
    directory: Path = field(default=None)

    @property
    def seed_dir(self):
        return self.directory / "seed"


@dataclass
class TranscriptStep:
    type: str
    text: str = ""
    path: str = ""
    content: str = ""
    title: str = ""
    kind: str = "edit"
    options: list = field(default_factory=list)
    decision: str = ""  # expected PermissionDecision.kind for request_permission
    exit_code: int = 0


@dataclass
class Transcript:
    steps: list
    participant_replies: list = field(default_factory=list)


STEP_TYPES = (
    "send_text",
    "end_turn",
    "request_permission",
    "write_artifact",
    "emit_marker",
    "hang_until_cancel",
    "exit",
)

_PERMISSION_KINDS = {
    "allow_once",
    "allow_always",
    "reject_once",
    "reject_always",
    "cancel",
}


def _require_mapping(data, source):
    if not isinstance(data, dict):
        raise ScenarioError(f"{source} must be a mapping at the top level")
    return data


def _require_str(value, field_name):
    if not isinstance(value, str) or not value.strip():
        raise ScenarioError(f"{field_name} must be a non-empty string")
    return value


def _validate_artifact_path(path, field_name):
    _require_str(path, field_name)
    if path.startswith("/") or ".." in Path(path).parts:
        raise ScenarioError(f"{field_name} must be repository-relative: {path!r}")
    return path


def _load_skill(raw) -> SkillMapping:
    if isinstance(raw, str):
        raise ScenarioError(
            "skill must be a mapping with name/source/ref/cli/bundle; "
            "the legacy bare-string form was removed (ADR-0011)"
        )
    raw = _require_mapping(raw, "skill")
    missing = [key for key in ("name", "source", "ref", "cli", "bundle") if key not in raw]
    if missing:
        raise ScenarioError(f"skill is missing required keys: {', '.join(missing)}")
    name = _require_str(raw["name"], "skill.name")
    source = _require_str(raw["source"], "skill.source")
    ref = _require_str(raw["ref"], "skill.ref")
    cli = _require_str(raw["cli"], "skill.cli")
    bundle_raw = raw["bundle"]
    if not isinstance(bundle_raw, list) or not bundle_raw:
        raise ScenarioError("skill.bundle must be a non-empty array")
    bundle = tuple(
        _require_str(item, f"skill.bundle[{i}]") for i, item in enumerate(bundle_raw)
    )
    if name not in bundle:
        raise ScenarioError(f"skill.name {name!r} must be a member of skill.bundle")
    return SkillMapping(name=name, source=source, ref=ref, cli=cli, bundle=bundle)


def load_scenario(directory) -> Scenario:
    directory = Path(directory)
    source = directory / "scenario.yaml"
    try:
        with open(source, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError as e:
        raise ScenarioError(f"scenario file not found: {source}") from e
    data = _require_mapping(data, str(source))

    missing = [key for key in REQUIRED_FIELDS if key not in data]
    if missing:
        raise ScenarioError(f"{source} is missing required fields: {', '.join(missing)}")

    name = _require_str(data["name"], "name")
    backend = data["backend"]
    if backend not in SUPPORTED_BACKENDS:
        raise ScenarioError(
            f"backend must be one of {', '.join(SUPPORTED_BACKENDS)}: {backend!r}"
        )
    skill = _load_skill(data["skill"])
    requirement = _require_str(data["requirement"], "requirement")

    timeout_seconds = data["timeout_seconds"]
    if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
        raise ScenarioError("timeout_seconds must be a positive integer")

    seed_repo = None
    seed_repo_raw = data.get("seed_repo")
    seed_dir = directory / "seed"
    if seed_repo_raw is not None:
        seed_repo_raw = _require_mapping(seed_repo_raw, "seed_repo")
        if seed_dir.is_dir():
            raise ScenarioError("seed/ and seed_repo are mutually exclusive; remove one")
        url = _require_str(seed_repo_raw.get("url"), "seed_repo.url")
        ref = _require_str(seed_repo_raw.get("ref"), "seed_repo.ref")
        if not _FULL_SHA_RE.match(ref):
            raise ScenarioError(
                f"seed_repo.ref must be a full commit SHA (40 or 64 hex chars); "
                f"branch and tag names are rejected: {ref!r}"
            )
        seed_repo = SeedRepo(url=url, ref=ref)

    artifacts_raw = data["expected_artifacts"]
    if not isinstance(artifacts_raw, list) or not artifacts_raw:
        raise ScenarioError("expected_artifacts must be a non-empty array")
    expected_artifacts = []
    for i, item in enumerate(artifacts_raw):
        item = _require_mapping(item, f"expected_artifacts[{i}]")
        path = _validate_artifact_path(item.get("path"), f"expected_artifacts[{i}].path")
        marker_line = item.get("marker_line")
        if marker_line:
            marker_line = _require_str(marker_line, f"expected_artifacts[{i}].marker_line")
        else:
            marker_line = None
        expected_artifacts.append(ExpectedArtifact(path=path, marker_line=marker_line))

    dimensions = data["llm_dimensions"]
    if not isinstance(dimensions, list) or not dimensions:
        raise ScenarioError("llm_dimensions must be a non-empty array")
    seen = set()
    llm_dimensions = []
    for i, dim in enumerate(dimensions):
        dim = _require_str(dim, f"llm_dimensions[{i}]")
        if dim in seen:
            raise ScenarioError(f"llm_dimensions contains a duplicate: {dim!r}")
        seen.add(dim)
        llm_dimensions.append(dim)

    user_brief = _require_str(data["user_brief"], "user_brief")

    spec_root = "docs/specs"
    if data.get("spec_root") is not None:
        try:
            spec_root = normalize_spec_root(data["spec_root"])
        except ConfigError as e:
            raise ScenarioError(f"spec_root is invalid: {e}") from e

    return Scenario(
        name=name,
        backend=backend,
        skill=skill,
        requirement=requirement,
        timeout_seconds=timeout_seconds,
        seed_repo=seed_repo,
        expected_artifacts=expected_artifacts,
        llm_dimensions=llm_dimensions,
        user_brief=user_brief,
        spec_root=spec_root,
        directory=directory,
    )


def load_transcript(path) -> Transcript:
    path = Path(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError as e:
        raise ScenarioError(f"transcript file not found: {path}") from e
    data = _require_mapping(data, str(path))

    participant_replies_raw = data.get("participant_replies", [])
    if not isinstance(participant_replies_raw, list):
        raise ScenarioError(f"{path} participant_replies must be an array")
    participant_replies = [
        _require_str(reply, f"participant_replies[{i}]")
        for i, reply in enumerate(participant_replies_raw)
    ]

    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ScenarioError(f"{path} must define a non-empty steps array")

    steps = []
    for i, item in enumerate(steps_raw):
        item = _require_mapping(item, f"steps[{i}]")
        step_type = item.get("type")
        if step_type not in STEP_TYPES:
            raise ScenarioError(
                f"steps[{i}].type must be one of {', '.join(STEP_TYPES)}: {step_type!r}"
            )
        label = f"steps[{i}]"
        step = TranscriptStep(type=step_type)
        if step_type == "send_text":
            step.text = _require_str(item.get("text"), f"{label}.text")
        elif step_type == "write_artifact":
            step.path = _validate_artifact_path(item.get("path"), f"{label}.path")
            step.content = _require_str(item.get("content"), f"{label}.content")
        elif step_type == "emit_marker":
            step.path = _validate_artifact_path(item.get("path"), f"{label}.path")
        elif step_type == "request_permission":
            step.title = _require_str(item.get("title"), f"{label}.title")
            step.kind = _require_str(item.get("kind"), f"{label}.kind")
            options = item.get("options")
            if not isinstance(options, list) or not options:
                raise ScenarioError(f"{label}.options must be a non-empty array")
            for j, opt in enumerate(options):
                opt = _require_mapping(opt, f"{label}.options[{j}]")
                _require_str(opt.get("optionId"), f"{label}.options[{j}].optionId")
                _require_str(opt.get("name"), f"{label}.options[{j}].name")
                kind = _require_str(opt.get("kind"), f"{label}.options[{j}].kind")
                if kind not in _PERMISSION_KINDS - {"cancel"}:
                    raise ScenarioError(
                        f"{label}.options[{j}].kind must be an approval option kind: {kind!r}"
                    )
            step.options = options
            decision = item.get("decision")
            if decision is not None:
                decision = _require_str(decision, f"{label}.decision")
                if decision not in _PERMISSION_KINDS:
                    raise ScenarioError(f"{label}.decision is not a valid decision kind: {decision!r}")
            step.decision = decision or ""
        elif step_type == "exit":
            exit_code = item.get("exit_code", 0)
            if not isinstance(exit_code, int):
                raise ScenarioError(f"{label}.exit_code must be an integer")
            step.exit_code = exit_code
        steps.append(step)

    turn_boundaries = sum(step.type == "end_turn" for step in steps)
    if turn_boundaries != len(participant_replies):
        raise ScenarioError(
            f"{path} must define one participant_replies entry per end_turn step "
            f"({len(participant_replies)} replies, {turn_boundaries} boundaries)"
        )
    return Transcript(steps=steps, participant_replies=participant_replies)

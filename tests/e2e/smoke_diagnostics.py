"""Failure-safe, credential-redacted archives for Backend Smoke attempts."""

import json
import traceback
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote


class SmokeDiagnostics:
    def __init__(self, result, env, tmp, archive, spec_root):
        self.result = result
        self.tmp = Path(tmp)
        self.archive = Path(archive)
        self.spec_root = spec_root
        self.errors = []
        self.simulator = {}
        self.transcript = []
        self.status = {}
        self.rubric = None
        self.run_env = None
        self.start_attempted = False
        self.verifier = None
        self.chat_id = ""
        self.since = 0
        self.secrets = sorted({
            variant
            for name, value in env.items()
            if value and any(word in name.upper() for word in ("SECRET", "TOKEN", "API_KEY", "PASSWORD"))
            for variant in (value, quote(value, safe=""), json.dumps(value)[1:-1])
        }, key=len, reverse=True)

    def redact(self, text):
        for secret in self.secrets:
            text = text.replace(secret, "<REDACTED>")
        return text

    def error(self, phase, error):
        self.errors.append({
            "phase": phase,
            "error": self.redact("".join(traceback.format_exception(type(error), error, error.__traceback__))),
        })

    def write(self, relative, text):
        target = self.archive / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.redact(text), encoding="utf-8")

    def write_json(self, relative, value):
        self.write(relative, json.dumps(value, ensure_ascii=False, indent=2))

    def collect(self):
        # Never archive user configuration, the environment, or arbitrary files
        # outside the isolated state and Spec Root (including symlink targets).
        roots = [
            (self.tmp / "state" / "im-align", Path("bridge")),
            (self.tmp / "workspace" / self.spec_root, Path("specs")),
        ]
        for root, destination in roots:
            if root.is_symlink() or not root.exists() or not root.resolve().is_relative_to(self.tmp.resolve()):
                continue
            for source in root.rglob("*"):
                if source.is_symlink() or not source.is_file():
                    continue
                if not source.resolve().is_relative_to(root.resolve()):
                    continue
                relative = source.relative_to(root)
                if destination == Path("bridge") and not (
                    relative.as_posix() in ("active.json", "history.jsonl")
                    or relative.parts[0] == "logs"
                ):
                    continue
                try:
                    self.write(destination / relative, source.read_text(encoding="utf-8", errors="replace"))
                except OSError as error:
                    self.error("archive", error)
        self.write_json("transcript.json", {
            "participant_mode": self.result.participant_mode,
            "thread_messages": self.transcript,
            "simulator": self.simulator,
        })
        self.write_json("status.json", self.status)
        if self.errors and not self.result.skipped:
            self.result.verdict = "fail"
            self.result.failure_cause = "harness_error"
        if self.rubric is not None:
            self.rubric["verdict"] = self.result.verdict
            self.rubric["failure_cause"] = self.result.failure_cause or None
            self.write_json("rubric.json", self.rubric)
        self.write_json("result.json", asdict(self.result))
        self.write_json("errors.json", self.errors)

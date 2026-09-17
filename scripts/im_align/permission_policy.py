"""Deterministic ACP permission policy for pre-coding Alignment Sessions."""

import logging
from pathlib import Path

from .acp.client import PermissionDecision

log = logging.getLogger("im_align.permission_policy")

_READ_ONLY_KINDS = frozenset({"read", "search", "fetch", "think"})
_TARGET_PATH_KEYS = frozenset(
    {"path", "file_path", "filepath", "file_path_uri", "filepathuri", "target", "uri"}
)


def _extract_target_path_candidates(raw_input):
    """Collect backend-specific file targets from one structured tool call."""
    candidates = []

    def scan(mapping, depth):
        for key, value in mapping.items():
            if isinstance(value, str) and value and str(key).lower() in _TARGET_PATH_KEYS:
                candidates.append(value)
            elif isinstance(value, dict) and depth > 0:
                scan(value, depth - 1)

    if isinstance(raw_input, str):
        if raw_input:
            candidates.append(raw_input)
    elif isinstance(raw_input, dict):
        scan(raw_input, 1)
    return candidates


class PermissionPolicy:
    """Allow read-only work and Spec edits; reject every other restricted action."""

    def __init__(self, cwd, spec_root):
        self._workspace = Path(cwd).resolve()
        self._spec_root_name = spec_root
        self._spec_root = self._resolve(spec_root)

    def _resolve(self, raw_path):
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self._workspace / candidate
        candidate = candidate.resolve(strict=False)
        try:
            candidate.relative_to(self._workspace)
        except ValueError:
            return None
        return candidate

    def decide(self, request):
        kind = request.kind.lower()
        if kind in _READ_ONLY_KINDS:
            return PermissionDecision("allow_once")
        if kind != "edit":
            log.warning(
                "rejected restricted operation by policy: kind=%s tool_call=%s",
                request.kind,
                request.tool_call_id,
            )
            return PermissionDecision("cancel")

        candidates = _extract_target_path_candidates(request.raw_input)
        if self._spec_root is None or not candidates:
            log.warning(
                "rejected edit with no verifiable Spec target: tool_call=%s",
                request.tool_call_id,
            )
            return PermissionDecision("cancel")
        for raw_path in candidates:
            target = self._resolve(raw_path)
            if target is None or target == self._spec_root or target.is_dir():
                return self._reject_edit(raw_path, request.tool_call_id)
            try:
                target.relative_to(self._spec_root)
            except ValueError:
                return self._reject_edit(raw_path, request.tool_call_id)

        log.info(
            "allowed Spec edit: targets=%s tool_call=%s root=%s",
            candidates,
            request.tool_call_id,
            self._spec_root_name,
        )
        return PermissionDecision("allow_once")

    def _reject_edit(self, raw_path, tool_call_id):
        log.warning(
            "rejected edit outside Spec Root: target=%s tool_call=%s root=%s",
            raw_path,
            tool_call_id,
            self._spec_root_name,
        )
        return PermissionDecision("cancel")

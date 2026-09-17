"""Completion-signal detection, ported from legacy/go/internal/session/completion.go.

The Agent decides when Alignment is complete and outputs one standalone line
identifying the primary Spec file: ``[ALIGNMENT_COMPLETE] <relative path>``.
Prompt contracts cannot constrain LLMs
with 100% reliability, so strict detection here is the hard boundary:
  - the marker must be on a line by itself, with surrounding whitespace allowed,
    and fenced code blocks are skipped;
  - the path must be repository-relative, rejecting absolute paths and ``../``
    escapes;
  - the target must resolve under the configured Spec Root;
  - the target must be a regular file whose ModTime is later than 5 seconds
    before Session start, rejecting historical Spec files.
"""

import os
import re
from datetime import datetime, timedelta

COMPLETION_SIGNAL = "[ALIGNMENT_COMPLETE]"
SPEC_FRESHNESS_GRACE = timedelta(seconds=5)

_LINE_RE = re.compile(r"^\s*\[ALIGNMENT_COMPLETE\]\s+(\S+)\s*$")


def parse_completion(text, repo_path, session_start, spec_root):
    """Return (spec_rel, marker_found, file_valid)."""
    in_fence = False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        spec_rel = m.group(1)
        return spec_rel, True, _spec_file_valid(
            repo_path, spec_rel, session_start, spec_root
        )
    return "", False, False


def _spec_file_valid(repo_path, rel, session_start, spec_root):
    if not repo_path or not rel or not spec_root or os.path.isabs(rel):
        return False
    abs_path = os.path.realpath(os.path.join(repo_path, rel))
    repo_abs = os.path.realpath(repo_path)
    root_abs = os.path.realpath(os.path.join(repo_abs, spec_root))
    try:
        root_inside_repo = os.path.commonpath([repo_abs, root_abs]) == repo_abs
        file_inside_root = os.path.commonpath([root_abs, abs_path]) == root_abs
    except ValueError:
        root_inside_repo = False
        file_inside_root = False
    if not root_inside_repo or not file_inside_root or abs_path == root_abs:
        return False
    if not os.path.isfile(abs_path):
        return False
    mtime = datetime.fromtimestamp(os.path.getmtime(abs_path))
    return mtime > session_start - SPEC_FRESHNESS_GRACE

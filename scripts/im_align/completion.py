"""Completion-signal detection, ported from legacy/go/internal/session/completion.go.

The Agent decides when Alignment is complete and outputs one standalone line:
``[ALIGNMENT_COMPLETE] <relative path>``. Prompt contracts cannot constrain LLMs
with 100% reliability, so strict detection here is the hard boundary:
  - the marker must be on a line by itself, with surrounding whitespace allowed,
    and fenced code blocks are skipped;
  - the path must be repository-relative, rejecting absolute paths and ``../``
    escapes;
  - the target must be a regular file whose ModTime is later than 5 seconds
    before Session start, rejecting historical Spec files.
"""

import os
import re
from datetime import datetime, timedelta

COMPLETION_SIGNAL = "[ALIGNMENT_COMPLETE]"
SPEC_FRESHNESS_GRACE = timedelta(seconds=5)

_LINE_RE = re.compile(r"^\s*\[ALIGNMENT_COMPLETE\]\s+(\S+)\s*$")


def parse_completion(text, repo_path, session_start):
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
        return spec_rel, True, _spec_file_valid(repo_path, spec_rel, session_start)
    return "", False, False


def _spec_file_valid(repo_path, rel, session_start):
    if not repo_path or not rel or os.path.isabs(rel):
        return False
    abs_path = os.path.realpath(os.path.join(repo_path, rel))
    repo_abs = os.path.realpath(repo_path)
    try:
        inside = os.path.commonpath([repo_abs, abs_path]) == repo_abs
    except ValueError:
        inside = False
    if not inside or abs_path == repo_abs:
        return False
    if not os.path.isfile(abs_path):
        return False
    mtime = datetime.fromtimestamp(os.path.getmtime(abs_path))
    return mtime > session_start - SPEC_FRESHNESS_GRACE

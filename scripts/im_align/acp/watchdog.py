"""Pausable Turn watchdog, ported from legacy/go/internal/acp/watchdog.go.

Only Agent working time is counted. The watchdog pauses while Approval is
pending for human interaction, then resumes with the remaining budget. A fixed
timeout would include Approval wait time in the Turn budget and has caused Turns
to fail before Approval timeout.

Issue #13: the previous design restarted a threading.Timer on every resume and
counted pause() calls; a duplicated pause or a lost restart made resume() skip
rearming, and the Turn hung past its budget with no timeout, no log, and no
card update (run-1789439169-73ee6b). This implementation uses a single
evaluation loop that recomputes the deadline from explicit state at most one
second after any transition, so no interleaving of pause/resume/stop can lose
the fire. pause()/resume() are idempotent by construction.

On top of the working-budget accounting, an absolute deadline backstops the
whole Turn: budget plus `absolute_slack` (the caller passes its Approval
timeout). Even if a permission handler hangs without its own timeout, the
watchdog still fires; normal Approvals resolve well inside the slack.
"""

import logging
import threading
import time
from datetime import timedelta

log = logging.getLogger("im_align.acp.watchdog")


class TurnWatchdog:
    def __init__(self, timeout: timedelta, fire, absolute_slack=timedelta(0)):
        self._timeout = timeout
        self._fire_cb = fire
        self._remaining = timeout.total_seconds()
        self._absolute_deadline = (
            time.monotonic() + timeout.total_seconds() + absolute_slack.total_seconds()
        )
        self._cv = threading.Condition()
        self._paused = False
        self._segment_start = time.monotonic()
        self._fired = False
        self._stopped = False
        self._loop = threading.Thread(
            target=self._run, name="im-align-turn-watchdog", daemon=True
        )
        self._loop.start()

    def _run(self):
        fired = False
        with self._cv:
            while not self._stopped and not self._fired:
                now = time.monotonic()
                if now >= self._absolute_deadline:
                    self._fired = True
                    fired = True
                    break
                if self._paused:
                    self._cv.wait(timeout=1.0)
                    continue
                left = self._remaining - (now - self._segment_start)
                if left <= 0:
                    self._fired = True
                    fired = True
                    break
                self._cv.wait(timeout=min(left, self._absolute_deadline - now, 1.0))
        if fired:
            log.info(
                "watchdog fired: Turn exceeded %.1fs working budget",
                self._timeout.total_seconds(),
            )
            self._fire_cb()

    def pause(self):
        with self._cv:
            if self._stopped or self._fired or self._paused:
                return
            self._remaining -= time.monotonic() - self._segment_start
            self._paused = True
            remaining = self._remaining
        # No notify needed: _run re-evaluates state at most 1.0 s after any
        # transition because its Condition.wait is always capped at 1.0 s.
        log.info("watchdog paused: %.1fs working budget remaining", remaining)

    def resume(self):
        with self._cv:
            if not self._paused or self._stopped or self._fired:
                return
            self._paused = False
            self._segment_start = time.monotonic()
            remaining = self._remaining
            self._cv.notify_all()
        log.info("watchdog resumed: %.1fs working budget remaining", remaining)

    def stop(self):
        with self._cv:
            self._stopped = True
            self._cv.notify_all()

    @property
    def fired(self):
        with self._cv:
            return self._fired

"""Pausable Turn watchdog, ported from legacy/go/internal/acp/watchdog.go.

Only Agent working time is counted. The watchdog pauses while Approval is
pending for human interaction, then resumes with the remaining budget. A fixed
timeout would include Approval wait time in the Turn budget and has caused Turns
to fail before Approval timeout.
"""

import threading
from datetime import datetime, timedelta


class TurnWatchdog:
    def __init__(self, timeout: timedelta, fire):
        self._timeout = timeout
        self._fire_cb = fire
        self._remaining = timeout
        self._cv = threading.Condition()
        self._segment_start = datetime.now()
        self._running = True
        self._pauses = 0
        self._fired = False
        self._stopped = False
        self._timer = threading.Timer(timeout.total_seconds(), self._on_fire)
        self._timer.daemon = True
        self._timer.start()

    def _on_fire(self):
        with self._cv:
            self._fired = True
        self._fire_cb()

    def pause(self):
        with self._cv:
            self._pauses += 1
            if not self._running or self._stopped or self._fired:
                return
            self._remaining -= datetime.now() - self._segment_start
            self._running = False
            self._timer.cancel()

    def resume(self):
        with self._cv:
            if self._pauses > 0:
                self._pauses -= 1
            if self._pauses > 0 or self._running or self._stopped or self._fired:
                return
            seconds = max(self._remaining.total_seconds(), 0.001)
            self._running = True
            self._segment_start = datetime.now()
            self._timer = threading.Timer(seconds, self._on_fire)
            self._timer.daemon = True
            self._timer.start()

    def stop(self):
        with self._cv:
            self._stopped = True
            self._timer.cancel()

    @property
    def fired(self):
        with self._cv:
            return self._fired

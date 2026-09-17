"""Single-deadline watchdog for one Agent Backend Turn."""

import logging
import threading
import time
from datetime import timedelta

log = logging.getLogger("im_align.acp.watchdog")


class TurnWatchdog:
    def __init__(self, timeout: timedelta, fire):
        self._timeout = timeout
        self._fire_cb = fire
        self._deadline = time.monotonic() + timeout.total_seconds()
        self._cv = threading.Condition()
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
                if now >= self._deadline:
                    self._fired = True
                    fired = True
                    break
                left = self._deadline - now
                if left <= 0:
                    self._fired = True
                    fired = True
                    break
                self._cv.wait(timeout=min(left, 1.0))
        if fired:
            log.info(
                "watchdog fired: Turn exceeded %.1fs working budget",
                self._timeout.total_seconds(),
            )
            self._fire_cb()

    def stop(self):
        with self._cv:
            self._stopped = True
            self._cv.notify_all()

    @property
    def fired(self):
        with self._cv:
            return self._fired

"""TurnWatchdog tests for a bounded Agent Backend Turn."""

import threading
import time
import unittest
from datetime import timedelta

from scripts.im_align.acp.watchdog import TurnWatchdog


def make_watchdog(seconds):
    fired = threading.Event()
    return (
        TurnWatchdog(timedelta(seconds=seconds), fired.set),
        fired,
    )


class TurnWatchdogTests(unittest.TestCase):
    def test_fires_within_budget_when_never_paused(self):
        watchdog, fired = make_watchdog(0.4)
        try:
            self.assertTrue(fired.wait(timeout=3.0), "watchdog did not fire within budget")
        finally:
            watchdog.stop()

    def test_stop_prevents_fire(self):
        watchdog, fired = make_watchdog(5.0)
        watchdog.stop()
        self.assertFalse(fired.wait(timeout=0.5))

    def test_stop_after_fire_is_safe(self):
        watchdog, fired = make_watchdog(0.2)
        try:
            self.assertTrue(fired.wait(timeout=3.0))
        finally:
            watchdog.stop()

    def test_fire_callback_runs_exactly_once(self):
        calls = []
        done = threading.Event()

        def fire():
            calls.append(1)
            if len(calls) > 1:
                done.set()

        watchdog = TurnWatchdog(timedelta(seconds=0.3), fire)
        try:
            time.sleep(1.0)
            self.assertFalse(done.wait(timeout=0.2), "fire callback ran more than once")
            self.assertEqual(len(calls), 1)
        finally:
            watchdog.stop()

if __name__ == "__main__":
    unittest.main()

"""TurnWatchdog regression tests for Issue #13.

Live incident (run-1789439169-73ee6b, 2026-09-15): after an Approval
pause/resume cycle the watchdog never fired and the Turn hung for 25+ minutes
until manual stop. The exact live interleaving could not be reproduced in
isolation; these tests lock the invariant it violated — after any pause/resume
sequence, a fire must still happen within the remaining working budget, and no
interleaving of pause/resume/stop may strand the watchdog.
"""

import threading
import time
import unittest
from datetime import timedelta

from scripts.im_align.acp.watchdog import TurnWatchdog


def make_watchdog(seconds, slack_seconds=0.0):
    fired = threading.Event()
    return (
        TurnWatchdog(
            timedelta(seconds=seconds),
            fired.set,
            absolute_slack=timedelta(seconds=slack_seconds),
        ),
        fired,
    )


class TurnWatchdogTests(unittest.TestCase):
    def test_fires_within_budget_when_never_paused(self):
        watchdog, fired = make_watchdog(0.4)
        try:
            self.assertTrue(fired.wait(timeout=3.0), "watchdog did not fire within budget")
        finally:
            watchdog.stop()

    def test_pause_excludes_wait_time_from_budget(self):
        watchdog, fired = make_watchdog(0.6)
        try:
            time.sleep(0.2)
            watchdog.pause()
            time.sleep(0.5)  # Approval wait must not consume Turn budget.
            watchdog.resume()
            start = time.monotonic()
            self.assertTrue(fired.wait(timeout=3.0), "watchdog did not fire after resume")
            self.assertLess(time.monotonic() - start, 0.6)
        finally:
            watchdog.stop()

    def test_unbalanced_double_pause_single_resume_still_fires(self):
        # Issue #13 fragile path: pause() counted calls, so a duplicated pause
        # made the matching resume() skip restarting the timer and the Turn
        # hung past its budget forever.
        watchdog, fired = make_watchdog(0.5)
        try:
            time.sleep(0.1)
            watchdog.pause()
            watchdog.pause()
            time.sleep(0.2)
            watchdog.resume()
            self.assertTrue(
                fired.wait(timeout=3.0),
                "unbalanced pause/resume stranded the watchdog; the Turn would hang",
            )
        finally:
            watchdog.stop()

    def test_resume_without_pause_is_noop(self):
        watchdog, fired = make_watchdog(0.4)
        try:
            watchdog.resume()
            time.sleep(0.6)
            self.assertTrue(fired.wait(timeout=3.0), "stray resume broke the watchdog")
        finally:
            watchdog.stop()

    def test_pause_after_stop_is_noop(self):
        watchdog, fired = make_watchdog(5.0)
        watchdog.stop()
        watchdog.pause()
        watchdog.resume()
        self.assertFalse(fired.wait(timeout=0.5))

    def test_pause_after_fire_is_noop(self):
        watchdog, fired = make_watchdog(0.2)
        try:
            self.assertTrue(fired.wait(timeout=3.0))
            watchdog.pause()
            watchdog.resume()
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
            watchdog.pause()
            watchdog.resume()
            watchdog.pause()
            watchdog.resume()
            time.sleep(1.0)
            self.assertFalse(done.wait(timeout=0.2), "fire callback ran more than once")
            self.assertEqual(len(calls), 1)
        finally:
            watchdog.stop()

    def test_absolute_deadline_backstop_fires_even_while_paused(self):
        # Issue #13 spec: a hard absolute deadline that survives pause/resume
        # accounting bugs. If a permission handler hangs without its own
        # timeout, the watchdog must still fire after budget + slack.
        watchdog, fired = make_watchdog(0.3, slack_seconds=0.4)
        try:
            watchdog.pause()
            start = time.monotonic()
            self.assertTrue(
                fired.wait(timeout=3.0),
                "absolute backstop did not fire while the watchdog was paused",
            )
            self.assertGreaterEqual(time.monotonic() - start, 0.3)
        finally:
            watchdog.stop()


if __name__ == "__main__":
    unittest.main()

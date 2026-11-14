"""When the status page says the server is back: estimated from the phases a restart walks through."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mod_browser.applier import DEFAULT_SECONDS, RESTART_PHASES, ApplyDesk, back_online_by, learn
from mod_browser.queue import Queue

T0 = datetime(2026, 11, 14, 4, 0, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class BackOnlineByTest(unittest.TestCase):
    def test_sums_the_rest_of_the_restart_from_the_current_phase(self):
        est = back_online_by("backing-up", iso(T0), iso(T0), DEFAULT_SECONDS)
        rest = sum(DEFAULT_SECONDS[p] for p in RESTART_PHASES[RESTART_PHASES.index("backing-up"):])
        self.assertEqual(est, iso(T0 + timedelta(seconds=rest)))

    def test_time_already_spent_in_a_phase_counts_down(self):
        later = T0 + timedelta(seconds=60)
        a = back_online_by("starting", iso(T0), iso(T0), DEFAULT_SECONDS)
        b = back_online_by("starting", iso(T0), iso(later), DEFAULT_SECONDS)
        self.assertEqual(a, b)  # the same instant, not pushed back by a minute

    def test_an_overrunning_phase_never_shows_a_time_in_the_past(self):
        late = T0 + timedelta(hours=1)
        est = back_online_by("starting", iso(T0), iso(late), DEFAULT_SECONDS)
        self.assertGreater(datetime.fromisoformat(est), late)

    def test_no_estimate_outside_a_restart(self):
        for phase in ("idle", "waiting", "online", "failed", "halted", "needs-recovery"):
            self.assertIsNone(back_online_by(phase, iso(T0), iso(T0), DEFAULT_SECONDS), phase)

    def test_bad_timestamps_give_no_estimate(self):
        self.assertIsNone(back_online_by("starting", None, iso(T0), DEFAULT_SECONDS))
        self.assertIsNone(back_online_by("starting", "nonsense", iso(T0), DEFAULT_SECONDS))


class LearnTest(unittest.TestCase):
    def test_a_finished_restart_moves_the_estimates_toward_what_it_took(self):
        starts = {"stopping": iso(T0), "backing-up": iso(T0 + timedelta(seconds=20)),
                  "test-booting": iso(T0 + timedelta(seconds=220)), "promoting": iso(T0 + timedelta(seconds=620)),
                  "starting": iso(T0 + timedelta(seconds=630))}
        learned = learn(DEFAULT_SECONDS, starts, iso(T0 + timedelta(seconds=930)))
        self.assertLess(learned["stopping"], DEFAULT_SECONDS["stopping"])        # took 20 s
        self.assertGreater(learned["test-booting"], DEFAULT_SECONDS["test-booting"])  # took 400 s
        self.assertEqual(learned["warning"], DEFAULT_SECONDS["warning"])         # not seen: unchanged

    def test_garbage_in_leaves_the_estimates_alone(self):
        self.assertEqual(learn(DEFAULT_SECONDS, {"stopping": "x"}, iso(T0)), DEFAULT_SECONDS)


class DeskEtaTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.now = T0
        self.desk = ApplyDesk(Queue(Path(tmp.name) / "q.db"), enabled=True, clock=lambda: iso(self.now))

    def test_status_carries_back_online_by_during_a_restart_and_not_after(self):
        self.desk._set_phase("stopping", "Stopping the server.", 1)
        self.assertIsNotNone(self.desk.status()["backOnlineBy"])
        self.desk._set_phase("online", "Batch 1 applied.", 1)
        self.assertIsNone(self.desk.status()["backOnlineBy"])

    def test_restart_now_request_gets_an_estimate_when_empty_does_not(self):
        self.desk.queue.kv_set("apply.requested", '{"mode": "now", "at": "%s"}' % iso(T0))
        self.assertIsNotNone(self.desk.status()["backOnlineBy"])
        self.desk.queue.kv_set("apply.requested", '{"mode": "when-empty", "at": "%s"}' % iso(T0))
        self.assertIsNone(self.desk.status()["backOnlineBy"])

    def test_a_finished_batch_teaches_the_next_estimate(self):
        for phase, secs in (("stopping", 0), ("backing-up", 10), ("test-booting", 20), ("promoting", 30),
                            ("starting", 40)):
            self.now = T0 + timedelta(seconds=secs)
            self.desk._set_phase(phase, "", 7)
        self.now = T0 + timedelta(seconds=50)
        self.desk._learn_from(7)
        self.assertLess(self.desk._durations()["test-booting"], DEFAULT_SECONDS["test-booting"])


if __name__ == "__main__":
    unittest.main()

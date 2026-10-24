import tempfile
import unittest
from pathlib import Path

from sats_engine import feed, terminal
from sats_engine.config import EngineConfig
from sats_engine.data import synthetic

BARS, SNAPS = synthetic.generate(n_bars=400, seed=2)
RECORDS = list(feed.run(feed.SignalFeed(EngineConfig(), 0.01, validation={"verdict": "FAIL"}), BARS, SNAPS))


class RenderTests(unittest.TestCase):
    def test_plain_frame_has_the_essentials(self):
        out = terminal.render(RECORDS, width=100, color=False, chain="ok")
        for text in ("PSYNDICA", "VALIDATION FAILED", "BTC/USD", "vs HODL", "TIMEFRAMES",
                     "ON-CHAIN", "SIGNALS", "never orders", "chain ✓", "walk-fwd FAIL"):
            self.assertIn(text, out)
        self.assertNotIn("\x1b[", out)

    def test_colour_only_when_asked(self):
        self.assertIn("\x1b[", terminal.render(RECORDS, color=True))

    def test_empty_feed_shows_how_to_start(self):
        self.assertIn("waiting for the first feed record", terminal.render([], color=False))

    def test_broken_chain_is_loud(self):
        self.assertIn("chain BROKEN: gap", terminal.render(RECORDS, color=False, chain="gap"))

    def test_v1_record_without_new_fields_still_renders(self):
        old = {k: v for k, v in RECORDS[-1].items() if k not in ("timeframes", "base_timeframe")}
        self.assertIn("POSTURE", terminal.render([old], color=False))

    def test_narrow_terminal_is_clamped(self):
        lines = terminal.render(RECORDS, width=20, color=False).splitlines()
        self.assertEqual(len(lines[1]), 60)


class HelperTests(unittest.TestCase):
    def test_tail_reads_only_the_end_and_skips_partial_lines(self):
        p = Path(tempfile.mkdtemp()) / "f.jsonl"
        w = feed.FeedWriter(p)
        for body in RECORDS:
            w.write(body)
        with open(p, "a") as f:
            f.write('{"partial": ')  # writer mid-append
        tail = terminal.tail_records(p, 5)
        self.assertEqual([r["seq"] for r in tail], list(range(len(RECORDS) - 3, len(RECORDS) + 1)))

    def test_tail_of_missing_file_is_empty(self):
        self.assertEqual(terminal.tail_records("/nonexistent/feed.jsonl", 5), [])

    def test_sparkline(self):
        self.assertEqual(terminal.sparkline([1, 1, 1], 10), "▄▄▄")
        self.assertEqual(terminal.sparkline([0, 1], 10), "▁█")
        self.assertEqual(len(terminal.sparkline(list(range(100)), 20)), 20)


if __name__ == "__main__":
    unittest.main()

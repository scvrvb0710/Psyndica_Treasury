import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sats_engine import backtest, feed, validate
from sats_engine.cli import main
from sats_engine.config import EngineConfig
from sats_engine.data import synthetic
from sats_engine.data.loaders import write_rows
from sats_engine.models import Bar

BARS, SNAPS = synthetic.generate(n_bars=24 * 200, seed=11)
SMALL = validate.ValidationParams(warmup_bars=240, train_bars=24 * 30, test_bars=24 * 15, min_trips=5)


def flat(n):
    return [Bar(i * 3600, 100, 100, 100, 100) for i in range(n)]


class ScoredBacktestTests(unittest.TestCase):
    def test_score_window_excludes_warmup(self):
        res = backtest.run(flat(300), score_from=250)
        self.assertEqual(res.edge_vs_hodl, 0.0)
        self.assertEqual(res.trips, 0)

    def test_score_from_must_index_bars(self):
        with self.assertRaises(ValueError):
            backtest.run(flat(10), score_from=10)


class WalkForwardTests(unittest.TestCase):
    def test_too_little_data_is_insufficient_not_pass(self):
        r = validate.walk_forward(BARS[:600], SNAPS, params=SMALL)
        self.assertEqual(r["verdict"], validate.INSUFFICIENT)
        self.assertEqual(r["folds"], [])

    def test_test_windows_follow_training_and_never_overlap(self):
        r = validate.walk_forward(BARS, SNAPS, params=SMALL)
        folds = r["folds"]
        self.assertGreaterEqual(len(folds), 3)
        self.assertEqual(folds[0]["test_from"], BARS[SMALL.warmup_bars + SMALL.train_bars].ts)
        for a, b in zip(folds, folds[1:]):
            self.assertEqual(b["test_from"], a["test_to"] + 3600)
        self.assertIn(r["verdict"], (validate.PASS, validate.FAIL, validate.INSUFFICIENT))
        self.assertEqual(r["data"]["sha256"], validate.data_fingerprint(BARS))

    def test_is_deterministic(self):
        a = validate.walk_forward(BARS, SNAPS, params=SMALL)
        b = validate.walk_forward(BARS, SNAPS, params=SMALL)
        self.assertEqual(a, b)

    def test_unknown_grid_parameter_rejected(self):
        with self.assertRaises(ValueError):
            validate.apply_overrides(EngineConfig(), {"swing.nope": 1})


def bodies(bars=BARS, snaps=SNAPS, **kw):
    f = feed.SignalFeed(EngineConfig(), 0.01, **kw)
    return list(feed.run(f, bars, snaps))


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def write_all(self, path, bs):
        w = feed.FeedWriter(path)
        return sum(w.write(b) is not None for b in bs)

    def test_one_record_per_bar_and_chain_verifies(self):
        p = self.tmp / "f.jsonl"
        self.assertEqual(self.write_all(p, bodies()), len(BARS))
        self.assertEqual(feed.verify(p), len(BARS))

    def test_resume_continues_chain_without_duplicates(self):
        whole, split = self.tmp / "whole.jsonl", self.tmp / "split.jsonl"
        self.write_all(whole, bodies())
        self.write_all(split, bodies(BARS[:1000]))
        self.assertEqual(self.write_all(split, bodies()), len(BARS) - 1000)
        self.assertEqual(split.read_bytes(), whole.read_bytes())

    def test_tampering_and_gaps_are_detected(self):
        p = self.tmp / "f.jsonl"
        self.write_all(p, bodies(BARS[:300]))
        lines = p.read_text().splitlines()
        rec = json.loads(lines[100])
        rec["stack"]["btc"] = 99.0
        tampered = lines[:100] + [json.dumps(rec)] + lines[101:]
        p.write_text("\n".join(tampered) + "\n")
        with self.assertRaisesRegex(feed.FeedError, "hash mismatch"):
            feed.verify(p)
        p.write_text("\n".join(lines[:100] + lines[101:]) + "\n")
        with self.assertRaisesRegex(feed.FeedError, "gap"):
            feed.verify(p)

    def test_signals_are_never_orders(self):
        actions = {s["action"] for b in bodies() for s in b["signals"]}
        self.assertTrue(actions)
        self.assertLessEqual(actions, {"would_sell", "would_rebuy"})

    def test_posture(self):
        last = lambda **kw: bodies(BARS[:400], **kw)[-1]["posture"]
        self.assertEqual(bodies(BARS[:400], snaps=[])[-1]["posture"], "stale_data")
        self.assertEqual(last(), "unvalidated_shadow")
        self.assertEqual(last(validation={"verdict": "FAIL"}), "validation_failed")
        self.assertEqual(last(validation={"verdict": "PASS"}), "validated_shadow")
        gov = SimpleNamespace(halted=False, mode="live", review_required=False, head="h", genesis_hash="g")
        self.assertEqual(last(validation={"verdict": "PASS"}, governance=gov), "live_eligible")
        gov.halted = True
        self.assertEqual(last(validation={"verdict": "PASS"}, governance=gov), "halted")

    def test_governance_halt_stops_simulated_trading(self):
        gov = SimpleNamespace(halted=True, mode="shadow", review_required=False, head="h", genesis_hash="g")
        self.assertTrue(all(not s.get("taken_in_sim") for b in bodies(governance=gov) for s in b["signals"]))

    def test_cli_validate_feed_and_verify(self):
        bars_csv, snaps_csv = self.tmp / "bars.csv", self.tmp / "snaps.csv"
        write_rows(bars_csv, BARS[:800], ["ts", "open", "high", "low", "close", "volume"])
        write_rows(snaps_csv, SNAPS[:800], ["ts", "fee_fast", "mempool_vsize", "mempool_count",
                                             "whale_btc", "block_fullness", "hashrate"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.assertEqual(main(["validate", "--prices", str(bars_csv), "--out", str(self.tmp / "v.json")]), 0)
            self.assertEqual(main(["feed", "run", "--prices", str(bars_csv), "--onchain", str(snaps_csv),
                                   "--validation", str(self.tmp / "v.json"), "--out", str(self.tmp / "f.jsonl")]), 0)
            self.assertEqual(main(["feed", "verify", str(self.tmp / "f.jsonl")]), 0)
        self.assertIn("verdict: INSUFFICIENT", out.getvalue())
        last = json.loads((self.tmp / "f.jsonl").read_text().splitlines()[-1])
        self.assertEqual(last["validation"]["verdict"], "INSUFFICIENT")
        self.assertEqual(last["seq"], 800)


if __name__ == "__main__":
    unittest.main()

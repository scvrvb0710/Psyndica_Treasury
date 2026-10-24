import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sats_engine import feed, validate
from sats_engine.cli import _apply_base, main
from sats_engine.config import EngineConfig
from sats_engine.data import synthetic
from sats_engine.data.loaders import write_rows
from sats_engine.governance.ledger import LedgerError
from sats_engine.models import Bar
from sats_engine.strategy.accumulator import HOLD_CAP_S, SatsRatchet
from sats_engine.strategy.multitf import MultiTimeframeRatchet, engine_class, warmup_hours
from sats_engine.timeframes import Resampler

M15, H4, D1 = 900, 4 * 3600, 24 * 3600


def mtf_config():
    cfg = EngineConfig()
    cfg.bar_seconds = M15
    return cfg


BARS, SNAPS = synthetic.generate(n_bars=96 * 120, seed=5, bar_seconds=M15, start_ts=1_700_006_400)


class ResamplerTests(unittest.TestCase):
    def test_sixteen_quarter_hours_make_one_4h_bar(self):
        r = Resampler(H4, M15)
        start = 1_700_006_400  # 4h-aligned
        out = []
        for i in range(16):
            out += r.push(Bar(start + i * M15, 100 + i, 110 + i, 90 - i, 101 + i, 1.0))
        self.assertEqual(out, [Bar(start, 100, 125, 75, 116, 16.0)])

    def test_unfinished_bar_is_not_emitted(self):
        r = Resampler(H4, M15)
        out = [b for i in range(15) for b in r.push(Bar(1_700_006_400 + i * M15, 1, 1, 1, 1))]
        self.assertEqual(out, [])

    def test_gap_flushes_partial_bucket(self):
        r = Resampler(H4, M15)
        r.push(Bar(1_700_006_400, 1, 2, 0.5, 1.5))
        out = r.push(Bar(1_700_006_400 + H4, 3, 3, 3, 3))
        self.assertEqual(out[0].close, 1.5)

    def test_rejects_non_multiple(self):
        with self.assertRaises(ValueError):
            Resampler(1000, M15)


class MultiTimeframeEngineTests(unittest.TestCase):
    def test_engine_selection_by_base(self):
        self.assertIs(engine_class(mtf_config()), MultiTimeframeRatchet)
        self.assertIs(engine_class(EngineConfig()), SatsRatchet)

    def run_engine(self, eng):
        j = 0
        for b in BARS:
            batch = []
            while j < len(SNAPS) and SNAPS[j].ts <= b.ts + M15:
                batch.append(SNAPS[j])
                j += 1
            eng.on_bar(b, batch)
        return eng

    def test_swing_trades_only_on_4h_closes_and_obey_72h_cap(self):
        eng = self.run_engine(MultiTimeframeRatchet(mtf_config(), 1.0))
        swings = [t for t in eng.closed + eng.open if t.kind == "swing"]
        self.assertTrue(swings)
        for t in swings:
            self.assertEqual((t.opened_ts + M15) % H4, 0)
            self.assertLessEqual(t.expires_ts - t.opened_ts, HOLD_CAP_S)

    def test_daily_uptrend_blocks_every_sell(self):
        eng = MultiTimeframeRatchet(mtf_config(), 1.0)
        eng.daily = SimpleNamespace(regime="trending_up", update=lambda bar: None)
        self.run_engine(eng)
        self.assertEqual(eng.closed + eng.open, [])
        self.assertEqual(eng.btc, 1.0)

    def test_warmup_covers_daily_filter(self):
        self.assertGreaterEqual(warmup_hours(mtf_config()), 50 * 24)
        self.assertLess(warmup_hours(EngineConfig()), 50 * 24)


class MultiTimeframeFeedTests(unittest.TestCase):
    def test_records_carry_all_three_timeframes(self):
        f = feed.SignalFeed(mtf_config(), 0.01)
        recs = list(feed.run(f, BARS, SNAPS))
        self.assertEqual(len(recs), len(BARS))
        last = recs[-1]
        self.assertEqual(last["schema_version"], 2)
        self.assertEqual(last["base_timeframe"], "15m")
        self.assertEqual(set(last["timeframes"]), {"15m", "4h", "1d"})
        self.assertNotEqual(last["timeframes"]["1d"]["regime"], "warming_up")
        self.assertEqual((last["timeframes"]["4h"]["bar_ts"]) % H4, 0)
        sells = [s for r in recs for s in r["signals"] if s["action"] == "would_sell"]
        self.assertTrue(all("blocked_by_htf" in s for s in sells))

    def test_single_timeframe_records_have_no_timeframe_block(self):
        bars, snaps = synthetic.generate(n_bars=300)
        rec = list(feed.run(feed.SignalFeed(EngineConfig(), 0.01), bars, snaps))[-1]
        self.assertIsNone(rec["timeframes"])
        self.assertEqual(rec["base_timeframe"], "1h")

    def test_validator_uses_long_enough_warmup(self):
        r = validate.walk_forward(BARS, SNAPS, mtf_config(),
                                  validate.ValidationParams(train_hours=24 * 30, test_hours=24 * 15, min_trips=1))
        self.assertEqual(r["method"]["base_timeframe"], "15m")
        self.assertGreaterEqual(r["method"]["warmup_hours"], 50 * 24)
        self.assertTrue(r["folds"])


class CliBaseTests(unittest.TestCase):
    def test_hourly_csv_refused_by_15m_engine(self):
        tmp = Path(tempfile.mkdtemp())
        bars, _ = synthetic.generate(n_bars=50)
        write_rows(tmp / "h.csv", bars, ["ts", "open", "high", "low", "close", "volume"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(["backtest", "--base", "15m", "--prices", str(tmp / "h.csv")])
        self.assertEqual(code, 2)
        self.assertIn("spaced 3600s apart", out.getvalue())

    def test_governed_base_cannot_be_overridden(self):
        with self.assertRaisesRegex(LedgerError, "needs a mandate"):
            _apply_base(EngineConfig(), SimpleNamespace(base="15m"), governed=True)
        cfg = _apply_base(EngineConfig(), SimpleNamespace(base="15m"), governed=False)
        self.assertEqual(cfg.bar_seconds, M15)


if __name__ == "__main__":
    unittest.main()

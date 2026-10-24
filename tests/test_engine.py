import math
import tempfile
import unittest
from pathlib import Path

from sats_engine import backtest
from sats_engine.config import EngineConfig, FeeModel, ScalpParams
from sats_engine.data import synthetic
from sats_engine.data.explorer import MempoolExplorer
from sats_engine.data.loaders import append_snapshot, load_onchain
from sats_engine.models import Bar, OnChainSnapshot, Ticket
from sats_engine.signals.onchain import NEUTRAL
from sats_engine.signals.technical import RSI, EfficiencyRatio, RollingStats
from sats_engine.strategy.accumulator import SatsRatchet
from sats_engine.strategy.scalp import ScalpSignal
from sats_engine.strategy.swing import TicketPlan


def bar(ts, o, h, l, c):
    return Bar(ts, o, h, l, c)


class IndicatorTests(unittest.TestCase):
    def test_efficiency_ratio_straight_line_is_one(self):
        er = EfficiencyRatio(10)
        for i in range(20):
            v = er.update(100 + i)
        self.assertAlmostEqual(v, 1.0)

    def test_efficiency_ratio_chop_is_near_zero(self):
        er = EfficiencyRatio(10)
        for i in range(21):
            v = er.update(100 + (1 if i % 2 else -1))
        self.assertLess(v, 0.11)

    def test_rsi_all_gains_is_100(self):
        rsi = RSI(14)
        for i in range(30):
            v = rsi.update(100 + i)
        self.assertEqual(v, 100.0)

    def test_rolling_stats_matches_naive(self):
        rs = RollingStats(5)
        xs = [3, 1, 4, 1, 5, 9, 2, 6]
        for x in xs:
            rs.update(x)
        window = xs[-5:]
        mean = sum(window) / 5
        std = math.sqrt(sum((x - mean) ** 2 for x in window) / 4)
        self.assertAlmostEqual(rs.mean, mean)
        self.assertAlmostEqual(rs.std, std)


class EdgeFloorTests(unittest.TestCase):
    def test_floor_respects_absolute_minimum(self):
        fees = FeeModel(maker=0, taker=0, slippage=0)
        self.assertEqual(fees.edge_floor(), 0.0015)

    def test_scalp_never_fires_when_fees_eat_the_edge(self):
        fees = FeeModel(maker=0.01, taker=0.01, slippage=0.0)
        sig = ScalpSignal(ScalpParams(er_max=1.0), fees, 0.0)
        fired = []
        for i in range(200):
            c = 100 + (3 if i % 7 == 0 else 0) + (i % 2)
            fired.append(sig.update(bar(i, c, c, c, c), NEUTRAL))
        self.assertTrue(all(p is None for p in fired))


class TicketAccountingTests(unittest.TestCase):
    def make_engine(self, **kw):
        cfg = EngineConfig(fees=FeeModel(maker=0.001, taker=0.002, slippage=0.0), **kw)
        return SatsRatchet(cfg, initial_btc=1.0)

    def open_ticket(self, eng, price=100.0, target=90.0, stop=110.0):
        plan = TicketPlan("swing", 1.0, target, stop, 72, "test")
        eng._open_ticket(bar(0, price, price, price, price), plan)
        self.assertEqual(len(eng.open), 1)
        return eng.open[0]

    def test_sell_respects_sleeve(self):
        eng = self.make_engine()
        t = self.open_ticket(eng)
        self.assertAlmostEqual(t.btc_sold, 0.30 * 0.25)  # sleeve x size_frac
        self.assertAlmostEqual(eng.btc + t.btc_sold, 1.0)

    def test_target_hit_gains_sats(self):
        eng = self.make_engine()
        t = self.open_ticket(eng)
        eng._manage_tickets(bar(3600, 95, 96, 89, 92))
        self.assertEqual(t.exit_reason, "target")
        self.assertGreater(t.sats_delta, 0)
        self.assertGreater(eng.btc, 1.0)

    def test_stop_hit_loses_bounded_sats(self):
        eng = self.make_engine()
        t = self.open_ticket(eng)
        eng._manage_tickets(bar(3600, 105, 111, 104, 109))
        self.assertEqual(t.exit_reason, "stop")
        self.assertLess(t.sats_delta, 0)
        # Loss ~ size * (1 - 100/110) plus fees; nowhere near the whole sleeve.
        self.assertLess(-t.sats_delta, t.btc_sold * 0.12)

    def test_both_levels_in_one_bar_assumes_stop(self):
        eng = self.make_engine()
        t = self.open_ticket(eng)
        eng._manage_tickets(bar(3600, 100, 115, 85, 100))
        self.assertEqual(t.exit_reason, "stop")

    def test_expiry_closes_ticket(self):
        eng = self.make_engine()
        t = self.open_ticket(eng)
        eng._manage_tickets(bar(72 * 3600, 100, 101, 99, 100))
        self.assertEqual(t.exit_reason, "expiry")
        self.assertEqual(eng.open, [])

    def test_max_open_total_is_one_by_default(self):
        eng = self.make_engine()
        self.open_ticket(eng)
        eng._open_ticket(bar(1, 100, 100, 100, 100), TicketPlan("scalp", 1.0, 95, 105, 12, "t"))
        self.assertEqual(len(eng.open), 1)

    def test_halt_blocks_new_tickets_until_resume(self):
        eng = self.make_engine(halt_drawdown=0.005)
        self.open_ticket(eng)
        # Price rockets: the USD in the ticket buys back far fewer sats.
        eng.on_bar(bar(3600, 100, 109, 100, 109))
        self.assertTrue(eng.halted)
        eng.open.clear()
        eng._open_ticket(bar(7200, 109, 109, 109, 109), TicketPlan("swing", 1.0, 100, 120, 72, "t"))
        self.assertEqual(eng.open, [])
        self.assertEqual(eng.decisions[-1]["why"], "halted")
        eng.resume()
        eng._open_ticket(bar(7200, 109, 109, 109, 109), TicketPlan("swing", 1.0, 100, 120, 72, "t"))
        self.assertEqual(len(eng.open), 1)

    def test_every_fill_carries_trace_id_matching_ticket(self):
        eng = self.make_engine()
        t = self.open_ticket(eng)
        eng._manage_tickets(bar(3600, 95, 96, 89, 92))
        self.assertEqual([f.trace_id for f in eng.fills], [t.trace_id, t.trace_id])


class BacktestTests(unittest.TestCase):
    def test_no_lookahead_on_onchain_data(self):
        seen = []

        class Spy(SatsRatchet):
            def on_bar(self, b, snaps=None):
                seen.append((b.ts, [s.ts for s in snaps or []]))
                super().on_bar(b, snaps)

        bars = [bar(i * 3600, 100, 100, 100, 100) for i in range(3)]
        snaps = [OnChainSnapshot(ts=3600), OnChainSnapshot(ts=3601), OnChainSnapshot(ts=10800)]
        import sats_engine.backtest as bt

        orig = bt.SatsRatchet
        bt.SatsRatchet = Spy
        try:
            bt.run(bars, snaps)
        finally:
            bt.SatsRatchet = orig
        self.assertEqual(seen, [(0, [3600]), (3600, [3601]), (7200, [10800])])

    def test_synthetic_run_is_deterministic_and_conserves_value(self):
        bars, snaps = synthetic.generate(n_bars=24 * 60, seed=3)
        a = backtest.run(bars, snaps, initial_btc=1.0)
        b = backtest.run(bars, snaps, initial_btc=1.0)
        self.assertEqual(a.final_stack_btc, b.final_stack_btc)
        self.assertGreater(a.final_stack_btc, 0.5)
        # Sats change must equal the sum of closed tickets plus open-ticket marks.
        eng = a.engine
        closed = sum(t.sats_delta for t in eng.closed)
        open_mark = sum(t.usd_held / bars[-1].close - t.btc_sold for t in eng.open)
        self.assertAlmostEqual(a.final_stack_btc - 1.0, closed + open_mark, places=9)

    def test_fees_only_backtest_never_beats_hodl_without_trades(self):
        bars = [bar(i * 3600, 100, 100, 100, 100) for i in range(500)]
        res = backtest.run(bars, initial_btc=1.0)
        self.assertEqual(res.final_stack_btc, 1.0)


class ExplorerTests(unittest.TestCase):
    def test_snapshot_parses_mempool_space_shapes(self):
        responses = {
            "/v1/fees/recommended": {"fastestFee": 42},
            "/mempool": {"vsize": 12_000_000, "count": 9000},
            "/v1/blocks": [{"weight": 3_996_000}, {"weight": 2_000_000}],
            "/mempool/recent": [
                {"txid": "a", "value": 250 * 10**8, "fee": 5000, "vsize": 250},
                {"txid": "b", "value": 1 * 10**8, "fee": 300, "vsize": 150},
            ],
            "/v1/mining/hashrate/3d": {"currentHashrate": 7.5e20},
        }
        ex = MempoolExplorer("http://node/api", fetch=lambda url: responses[url.split("/api", 1)[1]])
        s = ex.snapshot()
        self.assertEqual(s.fee_fast, 42)
        self.assertEqual((s.mempool_vsize, s.mempool_count), (12_000_000, 9000))
        self.assertAlmostEqual(s.block_fullness, (0.999 + 0.5) / 2)
        self.assertEqual(s.whale_btc, 250)
        self.assertAlmostEqual(s.hashrate, 750)

    def test_failing_endpoint_leaves_field_none(self):
        def fetch(url):
            if url.endswith("/mempool"):
                raise OSError("down")
            return {"fastestFee": 5} if "fees" in url else []

        s = MempoolExplorer("http://x/api", fetch=fetch).snapshot()
        self.assertEqual(s.fee_fast, 5)
        self.assertIsNone(s.mempool_vsize)
        self.assertIsNone(s.hashrate)

    def test_snapshot_csv_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "snaps.csv"
            append_snapshot(p, OnChainSnapshot(ts=1, fee_fast=3.0, mempool_count=10))
            append_snapshot(p, OnChainSnapshot(ts=2, whale_btc=120.5))
            snaps = load_onchain(p)
        self.assertEqual(snaps[0], OnChainSnapshot(ts=1, fee_fast=3.0, mempool_count=10))
        self.assertEqual(snaps[1], OnChainSnapshot(ts=2, whale_btc=120.5))


if __name__ == "__main__":
    unittest.main()

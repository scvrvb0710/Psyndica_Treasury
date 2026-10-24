import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sats_engine.cli import main
from sats_engine.data.rpc import BitcoinRPC, NodeExplorer, RPCError, load_auth


class FakeNode:
    """Speaks JSON-RPC like Knots/Core, including batch requests."""

    def __init__(self):
        self.ibd = False
        self.mempool = ["a", "b"]
        self.txs = {
            "a": [1.0], "b": [2.0],
            "whale": [150.0, 0.5], "shrimp": [0.1], "gone": None,
        }
        self.calls = []

    def result(self, method, params):
        self.calls.append(method)
        if method == "getblockchaininfo":
            return {"initialblockdownload": self.ibd, "verificationprogress": 0.42,
                    "blocks": 900_000, "pruned": True}
        if method == "estimatesmartfee":
            return {"feerate": 0.00012, "blocks": 2}
        if method == "getmempoolinfo":
            return {"bytes": 30_000_000, "size": 25_000}
        if method == "getblockcount":
            return 900_000
        if method == "getblockstats":
            return {"total_weight": 3_996_000 if params[0] % 2 == 0 else 2_000_000}
        if method == "getnetworkhashps":
            return 8.0e20
        if method == "getrawmempool":
            return list(self.mempool)
        if method == "getrawtransaction":
            vals = self.txs.get(params[0])
            if vals is None:
                raise KeyError("No such mempool transaction")
            return {"vout": [{"value": v} for v in vals]}
        raise KeyError(method)

    def handle(self, req):
        try:
            return {"id": req["id"], "result": self.result(req["method"], req["params"]), "error": None}
        except KeyError as e:
            return {"id": req["id"], "result": None, "error": {"code": -5, "message": str(e)}}

    def __call__(self, payload):
        if isinstance(payload, list):
            return [self.handle(r) for r in reversed(payload)]  # order not guaranteed
        return self.handle(payload)


class NodeExplorerTests(unittest.TestCase):
    def setUp(self):
        self.node = FakeNode()
        self.ex = NodeExplorer(BitcoinRPC(self.node), whale_threshold_btc=100, fullness_blocks=2)

    def test_snapshot_converts_units(self):
        s = self.ex.snapshot()
        self.assertAlmostEqual(s.fee_fast, 12.0)  # 0.00012 BTC/kvB = 12 sat/vB
        self.assertEqual((s.mempool_vsize, s.mempool_count), (30_000_000, 25_000))
        self.assertAlmostEqual(s.block_fullness, (0.999 + 0.5) / 2)
        self.assertAlmostEqual(s.hashrate, 800.0)
        self.assertIn("pruned=True", self.ex.status)

    def test_whale_flow_is_diff_between_scans(self):
        self.assertIsNone(self.ex.snapshot().whale_btc)  # baseline scan
        self.node.mempool = ["a", "whale", "shrimp", "gone"]
        self.assertAlmostEqual(self.ex.snapshot().whale_btc, 150.5)
        self.assertEqual(self.ex.snapshot().whale_btc, 0.0)  # nothing new

    def test_whale_sampling_caps_work(self):
        self.ex.max_new_txs = 2
        self.ex.snapshot()
        self.node.mempool = ["a", "b", "whale", "shrimp", "gone"]
        self.node.calls.clear()
        self.ex.snapshot()
        self.assertEqual(self.node.calls.count("getrawtransaction"), 2)

    def test_syncing_node_reports_nothing(self):
        self.node.ibd = True
        s = self.ex.snapshot()
        self.assertIsNone(s.fee_fast)
        self.assertIsNone(s.mempool_vsize)
        self.assertEqual(self.ex.status, "syncing 42.0%")

    def test_unreachable_node_reports_nothing(self):
        def down(payload):
            raise ConnectionRefusedError()

        ex = NodeExplorer(BitcoinRPC(down))
        self.assertIsNone(ex.snapshot().hashrate)
        self.assertIn("unreachable", ex.status)

    def test_fresh_node_without_fee_estimate(self):
        self.node.result = (lambda orig: lambda m, p: {"errors": ["Insufficient data"], "blocks": 0}
                            if m == "estimatesmartfee" else orig(m, p))(self.node.result)
        self.assertIsNone(self.ex.snapshot().fee_fast)

    def test_rpc_error_raises(self):
        with self.assertRaises(RPCError):
            BitcoinRPC(self.node).call("nosuchmethod")


class AuthTests(unittest.TestCase):
    def test_env_vars_win(self):
        with mock.patch.dict(os.environ, {"BITCOIN_RPC_USER": "u", "BITCOIN_RPC_PASSWORD": "p"}):
            self.assertEqual(load_auth("/nonexistent"), "u:p")

    def test_cookie_file(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {}, clear=True):
            p = Path(d) / ".cookie"
            p.write_text("__cookie__:abc123\n")
            self.assertEqual(load_auth(str(p)), "__cookie__:abc123")

    def test_missing_credentials_is_clean_cli_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main(["scan", "--rpc", "--rpc-cookie", "/nonexistent"]), 2)


if __name__ == "__main__":
    unittest.main()

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from sats_engine.cli import main
from sats_engine.data.collector import CollectorClient, CollectorError, load_secret, sign
from sats_engine.models import OnChainSnapshot

SECRET = "k" * 32
NOW = 1792840000


class FakeOpener:
    def __init__(self, fail_with=None, body=b"{}"):
        self.fail_with = fail_with
        self.body = body
        self.requests = []

    def __call__(self, req):
        self.requests.append(req)
        if self.fail_with:
            raise self.fail_with
        return self.body


def client(opener):
    return CollectorClient("https://c.example", SECRET, opener=opener, clock=lambda: NOW)


SNAP = OnChainSnapshot(ts=NOW - 60, fee_fast=12.5, mempool_count=25000)


class SigningTests(unittest.TestCase):
    def test_matches_worker_vector(self):
        # Same vector is asserted in collector/test/worker.test.mjs.
        self.assertEqual(
            sign(SECRET, 1792840000, "POST", "/ingest", '{"a":1}'),
            "12d8bb16c6f9306355a30d8c89f722a15f56af2475f7a6c644538179d9e6b64d",
        )

    def test_push_signs_exact_body(self):
        op = FakeOpener()
        self.assertEqual(client(op).push(SNAP, "abcdef0123456789", "rpc"), 1)
        req = op.requests[0]
        body = req.data.decode()
        self.assertEqual(json.loads(body), {
            "schema_version": 1, "trace_id": "abcdef0123456789", "source": "rpc",
            "snapshot": {"ts": NOW - 60, "fee_fast": 12.5, "mempool_count": 25000},
        })
        self.assertEqual(req.get_header("X-sats-timestamp"), str(NOW))
        self.assertEqual(req.get_header("X-sats-signature"), sign(SECRET, NOW, "POST", "/ingest", body))


class PushBehaviourTests(unittest.TestCase):
    def test_empty_snapshot_is_not_sent(self):
        op = FakeOpener()
        self.assertEqual(client(op).push(OnChainSnapshot(ts=NOW), "a" * 16, "rpc"), 0)
        self.assertEqual(op.requests, [])

    def test_network_failure_queues_then_flushes(self):
        op = FakeOpener(fail_with=urllib.error.URLError("down"))
        c = client(op)
        c.push(SNAP, "a" * 16, "rpc")
        c.push(SNAP, "b" * 16, "rpc")
        self.assertEqual(len(c.pending), 2)
        op.fail_with = None
        self.assertEqual(c.push(SNAP, "c" * 16, "rpc"), 3)
        sent = [json.loads(r.data)["trace_id"] for r in op.requests[-3:]]
        self.assertEqual(sent, ["a" * 16, "b" * 16, "c" * 16])  # order preserved

    def test_server_error_keeps_item_queued(self):
        err = urllib.error.HTTPError("u", 503, "busy", {}, io.BytesIO())
        c = client(FakeOpener(fail_with=err))
        c.push(SNAP, "a" * 16, "rpc")
        self.assertEqual(len(c.pending), 1)

    def test_rejection_drops_item_and_raises(self):
        err = urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO())
        c = client(FakeOpener(fail_with=err))
        with self.assertRaises(CollectorError):
            c.push(SNAP, "a" * 16, "rpc")
        self.assertEqual(len(c.pending), 0)

    def test_requires_https(self):
        with self.assertRaises(CollectorError):
            CollectorClient("http://c.example", SECRET)

    def test_export_signs_query(self):
        op = FakeOpener(body=b"ts,fee_fast\n1,2\n")
        self.assertEqual(client(op).export_csv("rpc", 5), "ts,fee_fast\n1,2\n")
        req = op.requests[0]
        self.assertTrue(req.full_url.endswith("/export?source=rpc&since=5"))
        self.assertEqual(req.get_header("X-sats-signature"),
                         sign(SECRET, NOW, "GET", "/export?source=rpc&since=5", ""))


class SecretHandlingTests(unittest.TestCase):
    def test_short_or_missing_secret_rejected(self):
        with mock.patch.dict(os.environ, {"SATS_COLLECTOR_SECRET": "short"}):
            with self.assertRaises(CollectorError):
                load_secret()

    def test_cli_fails_cleanly_without_secret(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main(["scan", "--push", "https://c.example"]), 2)
            with tempfile.TemporaryDirectory() as d:
                out = Path(d) / "x.csv"
                self.assertEqual(main(["collector-export", "--url", "https://c.example", "--out", str(out)]), 2)


if __name__ == "__main__":
    unittest.main()

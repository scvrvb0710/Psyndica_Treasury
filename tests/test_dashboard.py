import re
import tempfile
import unittest
from pathlib import Path

from sats_engine import dashboard, feed
from sats_engine.config import EngineConfig
from sats_engine.data import synthetic
from sats_engine.governance import ledger, rules

BARS, SNAPS = synthetic.generate(n_bars=400, seed=2)
RECORDS = list(feed.run(feed.SignalFeed(EngineConfig(), 0.01, validation={"verdict": "FAIL"}), BARS, SNAPS))
NOW = RECORDS[-1]["ts"] + 60
KEY = {"operator": "op", "keys": ["ssh-ed25519 AAAA"]}


def state(seated=("SUN", "WATER"), votes=None, title="Sleeve to 20%", llm=False, halted=False, dropped=()):
    s = ledger.State(genesis_hash="g" * 64, head="h" * 64,
                     roster={x: (dict(KEY, operator=x.lower()) if x in seated else None) for x in rules.SEATS},
                     params=ledger.default_params(), halted=halted)
    m = {"id": "0003", "class": "capital_change", "title": title, "rationale": "r", "changes": {},
         "drafted_with_llm": llm}
    s.records.append(ledger.Record("0003", "x" * 64, "capital_change", title,
                                   rules.Decision(False, "no quorum (1/8)"), dict(votes or {}), list(dropped),
                                   mandate=m))
    return s


def page(st=None, seat="WATER", gov_error=None, chain="ok", records=RECORDS, genesis="g" * 64):
    return dashboard.render_html(records, chain, st, gov_error, seat, "governance", genesis, NOW)


def levels(items):
    return [i.level for i in items]


class SafetyTests(unittest.TestCase):
    def test_strict_csp_and_no_scripts_or_remote_loads(self):
        out = page(state())
        self.assertIn("default-src 'none'", out)
        self.assertNotIn("<script", out.lower())
        self.assertIsNone(re.search(r"(src|href)=\"https?://", out))

    def test_hostile_mandate_title_is_escaped(self):
        out = page(state(title='<script>alert(1)</script><img src=x onerror=y>'))
        self.assertNotIn("<script>alert", out)
        self.assertNotIn("<img src=x", out)
        self.assertIn("&lt;script&gt;", out)

    def test_never_offers_orders_or_signing_itself(self):
        out = page(state()).lower()
        self.assertIn("never signs", out)
        for word in ("place order", "market order", "withdraw", "private key:"):
            self.assertNotIn(word, out)

    def test_ledger_failure_is_critical_and_treated_as_halted(self):
        items = dashboard.attention(RECORDS, "ok", None, "LedgerError: bad sig", "SUN", "governance", True, NOW)
        self.assertEqual(items[0].level, "critical")
        self.assertIn("treat the swarm as halted", items[0].detail)

    def test_broken_chain_is_critical(self):
        items = dashboard.attention(RECORDS, "gap at seq 4", None, None, None, "governance", True, NOW)
        self.assertEqual(items[0].level, "critical")
        self.assertIn("BROKEN", page(chain="gap at seq 4"))


class QueueTests(unittest.TestCase):
    def test_unvoted_seat_gets_action_with_sign_commands(self):
        items = dashboard.attention(RECORDS, "ok", state(), None, "WATER", "governance", True, NOW)
        act = [i for i in items if i.level == "action"]
        self.assertEqual(len(act), 1)
        cmds = "\n".join(act[0].commands)
        self.assertIn("gov vote 0003 --dir governance --seat WATER --vote <yes|no|abstain|veto>", cmds)
        self.assertIn("ssh-keygen -Y sign -n psyndica-mandate", cmds)
        self.assertIn("governance/votes/0003/WATER.json", cmds)

    def test_only_veto_seats_are_offered_veto(self):
        cmds = dashboard.vote_commands("governance", "0003", "SUN")
        self.assertIn("<yes|no|abstain>", cmds[0])

    def test_already_voted_is_info_not_action(self):
        items = dashboard.attention(RECORDS, "ok", state(votes={"WATER": "yes"}), None, "WATER", "governance",
                                    True, NOW)
        self.assertNotIn("action", levels(items))
        self.assertTrue(any("You voted YES" in i.detail for i in items))

    def test_unseated_seat_has_no_vote(self):
        items = dashboard.attention(RECORDS, "ok", state(), None, "MOON", "governance", True, NOW)
        self.assertNotIn("action", levels(items))

    def test_seat_filled_by_the_open_mandate_cannot_vote_on_it(self):
        st = state()
        rec = st.records[-1]
        rec.cls, rec.applied = "council_change", True
        rec.mandate["changes"] = {"seats.WATER": KEY}
        items = dashboard.attention(RECORDS, "ok", st, None, "WATER", "governance", True, NOW)
        self.assertNotIn("action", levels(items))
        self.assertEqual(dashboard.seated_for(st, rec), {"SUN"})

    def test_llm_drafted_mandate_is_flagged(self):
        items = dashboard.attention(RECORDS, "ok", state(llm=True), None, "WATER", "governance", True, NOW)
        self.assertIn("Drafted with an LLM", [i for i in items if i.level == "action"][0].detail)
        self.assertIn("[llm-drafted]", page(state(llm=True)))

    def test_dropped_vote_warns_its_owner(self):
        st = state(dropped=["WATER: bad signature"])
        items = dashboard.attention(RECORDS, "ok", st, None, "WATER", "governance", True, NOW)
        self.assertTrue(any(i.level == "warn" and "dropped" in i.title for i in items))

    def test_halted_swarm_shows_no_halt_card_and_no_restart_command(self):
        out = page(state(halted=True), seat="SUN")
        self.assertIn("Swarm is HALTED", out)
        self.assertNotIn("Emergency halt", out)
        self.assertNotIn("--class restart", out)

    def test_seated_operator_gets_halt_card(self):
        self.assertIn("--class halt", page(state(), seat="SUN"))

    def test_quoted_ledger_dir(self):
        cmds = dashboard.vote_commands("my ledger", "0003", "SUN")
        self.assertIn("--dir 'my ledger'", cmds[0])


class PanelTests(unittest.TestCase):
    def test_sections_render(self):
        out = page(state())
        for text in ("Needs you", "Shadow book", "Data health", "Signals", "Council", "Hashira 12",
                     "never orders", "verified", "gov status --dir governance --expect-genesis"):
            self.assertIn(text, out)

    def test_unpinned_genesis_warns(self):
        items = dashboard.attention(RECORDS, "ok", state(), None, "WATER", "governance", False, NOW)
        self.assertTrue(any(i.title == "Genesis not pinned" for i in items))

    def test_no_ledger_and_empty_feed_still_render(self):
        out = page(None, seat=None, records=[], chain=None)
        self.assertIn("No ledger attached", out)
        self.assertIn("NO FEED", out)

    def test_quiet_feed_warns(self):
        items = dashboard.attention(RECORDS, "ok", None, None, None, "governance", True, NOW + 3 * 86400)
        self.assertTrue(any(i.title.startswith("Feed hasn't updated in 3d") for i in items))
        fresh = dashboard.attention(RECORDS, "ok", None, None, None, "governance", True, NOW)
        self.assertFalse(any("hasn't updated" in i.title for i in fresh))

    def test_drawdown(self):
        recs = [{"stack": {"btc": b}} for b in (1.0, 1.2, 1.05)]
        self.assertAlmostEqual(dashboard.drawdown(recs), 1.05 / 1.2 - 1)
        self.assertIsNone(dashboard.drawdown([{}]))
        crit = [{"stack": {"btc": b}, "ts": 0} for b in (1.0, 0.85)]
        items = dashboard.attention(crit, "ok", None, None, None, "governance", True, NOW)
        self.assertTrue(any(i.level == "critical" and "halt line" in i.title for i in items))


class BuildTests(unittest.TestCase):
    def test_build_writes_file_and_reports_ledger_errors(self):
        tmp = Path(tempfile.mkdtemp())
        fpath = tmp / "feed.jsonl"
        w = feed.FeedWriter(fpath)
        for body in RECORDS[-50:]:
            w.write(body)

        def boom(d, g):
            raise ledger.LedgerError("genesis is not validly signed by SUN")

        out = tmp / "dash.html"
        rc = dashboard.run(str(fpath), str(out), "governance", None, "SUN", False, 30, True, 500, boom)
        self.assertEqual(rc, 0)
        text = out.read_text()
        self.assertIn("Ledger did not verify", text)
        self.assertIn("genesis is not validly signed by SUN", text)
        self.assertIn("verified", text)
        self.assertEqual([p.name for p in tmp.iterdir() if p.name.startswith(".dashboard-")], [])


if __name__ == "__main__":
    unittest.main()

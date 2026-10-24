import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sats_engine.cli import main
from sats_engine.governance import ledger, rules
from sats_engine.governance.ledger import LedgerError
from sats_engine.governance.sshsig import NAMESPACE, SSHVerifier, normalize_key, SignatureError

SEATS = rules.SEATS
HAVE_SSH = shutil.which("ssh-keygen") is not None
ALL = set(SEATS)


def yes(*seats, **others):
    v = {s: "yes" for s in seats}
    v.update(others)
    return v


class RulesTests(unittest.TestCase):
    def test_any_single_seat_can_halt_and_it_cannot_be_vetoed(self):
        d = rules.evaluate("halt", {"MIST": "yes", "WATER": "veto"}, ALL)
        self.assertTrue(d.accepted)

    def test_founding_phase_only_sun_seats_operators(self):
        self.assertTrue(rules.evaluate("council_change", yes("SUN"), {"SUN"}, founding=True).accepted)
        self.assertFalse(rules.evaluate("council_change", yes("FLAME"), {"SUN", "FLAME"}, founding=True).accepted)
        d = rules.evaluate("ordinary", yes("SUN"), {"SUN"}, founding=True)
        self.assertFalse(d.accepted)
        self.assertIn("founding phase", d.reason)

    def test_founding_returns_if_council_shrinks_below_supermajority(self):
        self.assertTrue(rules.in_founding_phase(set(SEATS[:8]), constituted=True))
        self.assertFalse(rules.in_founding_phase(set(SEATS[:9]), constituted=True))
        self.assertTrue(rules.in_founding_phase(ALL - {"MOON"}, constituted=False))

    def test_quorum(self):
        d = rules.evaluate("ordinary", yes(*SEATS[:7]), ALL)
        self.assertFalse(d.accepted)
        self.assertIn("no quorum", d.reason)

    def test_ordinary_majority(self):
        self.assertTrue(rules.evaluate("ordinary", yes(*SEATS[:5], MOON="no", SUN="no", LOVE="no"), ALL).accepted)
        self.assertFalse(rules.evaluate("ordinary", yes(*SEATS[:4], MOON="no", SUN="no", LOVE="no", SOUND="no"),
                                        ALL).accepted)
        # abstentions count toward quorum but not the majority
        self.assertTrue(rules.evaluate("ordinary", yes("FLAME", MIST="abstain", WIND="abstain", SUN="abstain",
                                                       LOVE="abstain", MOON="abstain", INSECT="abstain",
                                                       SOUND="abstain"), ALL).accepted)

    def test_supermajority_needs_nine(self):
        self.assertFalse(rules.evaluate("capital_change", yes(*SEATS[:8]), ALL).accepted)
        self.assertTrue(rules.evaluate("capital_change", yes(*SEATS[:9]), ALL).accepted)

    def test_go_live_needs_sun(self):
        no_sun = yes(*[s for s in SEATS if s != "SUN"])
        self.assertFalse(rules.evaluate("go_live", no_sun, ALL).accepted)
        self.assertTrue(rules.evaluate("go_live", yes(*SEATS), ALL).accepted)

    def test_water_and_stone_veto_beat_any_majority(self):
        for seat in ("WATER", "STONE"):
            d = rules.evaluate("ordinary", yes(*[s for s in SEATS if s != seat], **{seat: "veto"}), ALL)
            self.assertFalse(d.accepted)
            self.assertEqual(d.vetoed_by, (seat,))

    def test_other_seats_have_no_veto(self):
        d = rules.evaluate("ordinary", yes(*SEATS[:8], FLAME="veto"), ALL)
        self.assertIn("FLAME has no veto", d.reason)
        self.assertTrue(d.accepted)  # 7 yes vs 1 no

    def test_override_needs_sun_and_written_reason(self):
        self.assertFalse(rules.evaluate("override", yes("FLAME"), ALL, "a long enough written reason").accepted)
        self.assertFalse(rules.evaluate("override", yes("SUN"), ALL, "too short").accepted)
        self.assertTrue(rules.evaluate("override", yes("SUN"), ALL, "risk reviewed; veto was on stale data").accepted)


class KeyParsingTests(unittest.TestCase):
    def test_strips_comment_and_rejects_injection(self):
        self.assertEqual(normalize_key("ssh-ed25519 AAAAC3Nz me@host"), "ssh-ed25519 AAAAC3Nz")
        for bad in ("ssh-rsa AAAAB3", 'cert-authority ssh-ed25519 AAAA', "ssh-ed25519 AAAA\nSUN ssh-ed25519 BBBB",
                    'ssh-ed25519 AAAA" namespaces="*'):
            with self.assertRaises(SignatureError):
                normalize_key(bad)


@unittest.skipUnless(HAVE_SSH, "ssh-keygen not installed")
class LedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keydir = Path(tempfile.mkdtemp())
        for name in [*SEATS, "INTRUDER"]:
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name,
                            "-f", str(cls.keydir / name)], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.keydir)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    # helpers ---------------------------------------------------------------

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(list(argv))
        return code, out.getvalue()

    def gov(self, *argv):
        code, out = self.cli("gov", *argv[:1], "--dir", str(self.dir), *argv[1:])
        self.assertEqual(code, 0, out)
        return out

    def sign(self, path: Path, key: str):
        Path(str(path) + ".sig").unlink(missing_ok=True)
        subprocess.run(["ssh-keygen", "-Y", "sign", "-n", NAMESPACE, "-f", str(self.keydir / key), str(path)],
                       check=True, capture_output=True)

    def pub(self, seat):
        return str(self.keydir / f"{seat}.pub")

    def vote(self, mid, votes: dict, signer=None):
        for seat, v in votes.items():
            self.gov("vote", mid, "--seat", seat, "--vote", v)
            self.sign(self.dir / "votes" / mid / f"{seat}.json", signer or seat)

    def propose(self, cls, *extra, title="t", rationale="because"):
        self.gov("propose", "--class", cls, "--title", title, "--rationale", rationale, *extra)

    def last_id(self):
        return f"{len(list((self.dir / 'mandates').glob('*.json'))):04d}"

    def state(self, **kw):
        return ledger.load(self.dir, SSHVerifier(), **kw)

    def found_council(self):
        """Genesis + SUN seats the other 11 in one mandate."""
        self.gov("init", "--operator", "ezekiel", "--key", self.pub("SUN"))
        self.sign(self.dir / "genesis.json", "SUN")
        sets = []
        for seat in SEATS[:-1]:
            entry = {"operator": seat.lower() + "-op", "keys": [(self.keydir / f"{seat}.pub").read_text().strip()]}
            sets += ["--set", f"seats.{seat}={json.dumps(entry)}"]
        self.propose("council_change", *sets, title="seat the founding council")
        self.vote(self.last_id(), {"SUN": "yes"})

    # tests -----------------------------------------------------------------

    def test_founding_then_supermajority_capital_change_flows_into_engine(self):
        self.found_council()
        s = self.state()
        self.assertFalse(s.founding)
        self.assertEqual(len(s.seated), 12)

        self.propose("capital_change", "--set", "engine.sleeve_frac=0.2", "--set", "swing.entry_z=2.5")
        self.vote(self.last_id(), yes(*SEATS[:9]))
        s = self.state()
        self.assertTrue(s.records[-1].applied, s.records[-1].decision.reason)
        self.assertEqual(s.config().sleeve_frac, 0.2)
        self.assertEqual(s.config().swing.entry_z, 2.5)

        code, out = self.cli("demo", "--bars", "400", "--trades", "0", "--mandates", str(self.dir))
        self.assertEqual(code, 0, out)
        self.assertIn("governed by ledger head", out)

    def test_eight_yes_is_not_enough_for_capital_change(self):
        self.found_council()
        self.propose("capital_change", "--set", "engine.sleeve_frac=0.5")
        self.vote(self.last_id(), yes(*SEATS[:8]))
        s = self.state()
        self.assertFalse(s.records[-1].applied)
        self.assertEqual(s.config().sleeve_frac, 0.30)

    def test_forged_vote_is_dropped(self):
        self.found_council()
        self.propose("ordinary", "--set", "swing.entry_rsi=70")
        mid = self.last_id()
        self.vote(mid, yes(*[s for s in SEATS if s != "WATER"][:7]))
        self.vote(mid, {"WATER": "yes"}, signer="INTRUDER")
        rec = self.state().records[-1]
        self.assertNotIn("WATER", rec.votes)
        self.assertTrue(any("signature invalid" in d for d in rec.dropped_votes))
        self.assertIn("no quorum (7/8)", rec.decision.reason)

    def test_editing_a_voted_mandate_invalidates_its_votes(self):
        self.found_council()
        self.propose("ordinary", "--set", "swing.entry_rsi=70")
        mid = self.last_id()
        self.vote(mid, yes(*SEATS[:8]))
        path = self.dir / "mandates" / f"{mid}.json"
        m = json.loads(path.read_text())
        m["changes"]["swing.entry_rsi"] = 99.0
        ledger.write_canonical(path, m)
        rec = self.state().records[-1]
        self.assertFalse(rec.applied)
        self.assertEqual(rec.votes, {})

    def test_rewriting_history_breaks_the_chain(self):
        self.found_council()
        self.propose("ordinary", "--set", "swing.entry_rsi=70")
        path = self.dir / "mandates" / "0001.json"
        m = json.loads(path.read_text())
        m["title"] = "something else"
        ledger.write_canonical(path, m)
        with self.assertRaisesRegex(LedgerError, "chain broken"):
            self.state()

    def test_deleting_a_sealed_vote_cannot_unpass_a_mandate(self):
        self.found_council()
        self.propose("ordinary", "--set", "swing.entry_rsi=70")  # seals 0001's votes
        (self.dir / "votes" / "0001" / "SUN.json.sig").unlink()
        with self.assertRaisesRegex(LedgerError, "votes on 0001 changed after they were sealed"):
            self.state()

    def test_late_votes_cannot_pass_a_closed_mandate(self):
        self.found_council()
        self.propose("ordinary", "--set", "swing.entry_rsi=70")
        self.vote("0002", yes(*SEATS[:3]))
        self.propose("ordinary", "--set", "swing.entry_z=3.0")  # closes voting on 0002
        self.assertFalse(self.state().records[1].applied)
        self.vote("0002", yes(*SEATS[3:9]))
        with self.assertRaisesRegex(LedgerError, "votes on 0002 changed after they were sealed"):
            self.state()

    def test_genesis_must_be_signed_and_match_the_pin(self):
        self.gov("init", "--operator", "ezekiel", "--key", self.pub("SUN"))
        with self.assertRaisesRegex(LedgerError, "not validly signed"):
            self.state()
        self.sign(self.dir / "genesis.json", "INTRUDER")
        with self.assertRaisesRegex(LedgerError, "not validly signed"):
            self.state()
        self.sign(self.dir / "genesis.json", "SUN")
        pin = self.state().genesis_hash
        self.assertEqual(self.state(expect_genesis=pin).genesis_hash, pin)
        with self.assertRaisesRegex(LedgerError, "does not match the pinned"):
            self.state(expect_genesis="0" * 64)

    def test_non_canonical_files_are_rejected(self):
        self.found_council()
        path = self.dir / "mandates" / "0001.json"
        path.write_text(json.dumps(json.loads(path.read_text()), indent=2))
        with self.assertRaisesRegex(LedgerError, "not canonical"):
            self.state()

    def test_water_veto_then_sun_override_in_writing(self):
        self.found_council()
        self.propose("ordinary", "--set", "swing.entry_rsi=72")
        target = self.last_id()
        self.vote(target, yes(*[s for s in SEATS if s != "WATER"], WATER="veto"))
        s = self.state()
        self.assertEqual(s.records[-1].decision.vetoed_by, ("WATER",))
        self.assertEqual(s.config().swing.entry_rsi, 68.0)

        self.propose("override", "--target", target,
                     rationale="Water's veto cited stale fee data; reviewed with Water and Moon.")
        self.vote(self.last_id(), {"SUN": "yes"})
        s = self.state()
        self.assertEqual(s.config().swing.entry_rsi, 72.0)
        self.assertTrue(s.review_required)
        self.assertEqual(s.records[int(target) - 1].overridden_by, self.last_id())

    def test_override_cannot_rescue_a_mandate_that_lacked_votes(self):
        self.found_council()
        self.propose("capital_change", "--set", "engine.sleeve_frac=0.9")
        target = self.last_id()
        self.vote(target, yes(*SEATS[:5], WATER="veto", STONE="veto"))
        self.propose("override", "--target", target, rationale="I want this regardless of the council.")
        self.vote(self.last_id(), {"SUN": "yes"})
        self.assertEqual(self.state().config().sleeve_frac, 0.30)

    def test_one_seat_halts_restart_needs_the_council(self):
        self.found_council()
        self.propose("halt", title="anomaly on venue")
        self.vote(self.last_id(), {"SOUND": "yes"})
        s = self.state()
        self.assertTrue(s.halted)
        with self.assertRaisesRegex(LedgerError, "HALTED"):
            s.require_live()
        self.propose("restart")
        self.vote(self.last_id(), {"SOUND": "yes"})
        self.assertTrue(self.state().halted)  # one seat can't restart
        self.vote(self.last_id(), yes(*SEATS[:8]))
        self.assertFalse(self.state().halted)

    def test_going_live_needs_nine_and_sun(self):
        self.found_council()
        self.propose("go_live", "--set", "mode=live")
        self.vote(self.last_id(), yes(*[s for s in SEATS if s != "SUN"]))
        s = self.state()
        self.assertEqual(s.mode, "shadow")
        with self.assertRaisesRegex(LedgerError, "shadow"):
            s.require_live()
        self.vote(self.last_id(), {"SUN": "yes"})
        s = self.state()
        self.assertEqual(s.mode, "live")
        s.require_live()

    def test_class_must_match_what_is_changed(self):
        self.found_council()
        code, out = self.cli("gov", "propose", "--dir", str(self.dir), "--class", "ordinary",
                             "--title", "sneak", "--rationale", "r", "--set", "engine.sleeve_frac=0.9")
        self.assertEqual(code, 2)
        self.assertIn("needs class capital_change", out)
        code, out = self.cli("gov", "propose", "--dir", str(self.dir), "--class", "ordinary",
                             "--title", "x", "--rationale", "r", "--set", "engine.nope=1")
        self.assertEqual(code, 2)
        self.assertIn("unknown parameter", out)

    def test_hand_written_bad_mandate_is_recorded_as_invalid(self):
        self.found_council()
        mid, prev, seal = ledger.next_mandate_id(self.dir)
        ledger.write_canonical(self.dir / "mandates" / f"{mid}.json", {
            "schema_version": 1, "id": mid, "prev": prev, "class": "ordinary", "title": "sneak",
            "rationale": "r", "changes": {"mode": "live"}, "sealed_votes": seal,
        })
        self.vote(mid, yes(*SEATS))
        rec = self.state().records[-1]
        self.assertFalse(rec.applied)
        self.assertIn("needs class go_live", rec.decision.reason)

    def test_governed_run_refuses_cli_overrides(self):
        self.found_council()
        code, out = self.cli("demo", "--bars", "100", "--mandates", str(self.dir), "--max-open", "5")
        self.assertEqual(code, 2)
        self.assertIn("can't override a governed config", out)

    def test_status_reports_founding_and_llm_drafts(self):
        self.gov("init", "--operator", "ezekiel", "--key", self.pub("SUN"))
        self.sign(self.dir / "genesis.json", "SUN")
        self.propose("ordinary", "--set", "swing.entry_rsi=70", "--llm")
        out = self.gov("status")
        self.assertIn("FOUNDING PHASE", out)
        self.assertIn("[llm-drafted]", out)
        self.assertIn("only council_change allowed", out)


if __name__ == "__main__":
    unittest.main()

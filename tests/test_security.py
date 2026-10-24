"""Security guardrails enforced as tests. See SECURITY.md."""

import ast
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "sats_engine"

SECRET_PATTERNS = {
    "PEM private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "BIP32 extended private key": re.compile(r"\b[xyzt]prv[1-9A-HJ-NP-Za-km-z]{100,}\b"),
    "WIF private key": re.compile(r"\b[5KL][1-9A-HJ-NP-Za-km-z]{50,51}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "Generic API secret": re.compile(r"\bsk_(live|test)_[A-Za-z0-9]{16,}\b"),
    "Bitcoin RPC cookie": re.compile(r"__cookie__:[0-9a-f]{64}"),
}

BANNED_CALLS = {"eval", "exec"}
BANNED_IMPORTS = {"pickle", "marshal", "subprocess"}
# Reviewed exceptions (SECURITY.md): file -> imports it may use.
ALLOWED_IMPORTS = {
    # Mandate signature checks run OpenSSH ssh-keygen: fixed argv, no shell, timeout.
    ENGINE / "governance" / "sshsig.py": {"subprocess"},
}


def tracked_files():
    out = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    return [ROOT / p for p in out if (ROOT / p).is_file()]


class NoSecretsInTree(unittest.TestCase):
    def test_no_secret_patterns(self):
        hits = []
        for path in tracked_files():
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            for name, pat in SECRET_PATTERNS.items():
                if path.name == "test_security.py":
                    continue
                for m in pat.finditer(text):
                    hits.append(f"{path.relative_to(ROOT)}: {name}: {m.group()[:12]}...")
        self.assertEqual(hits, [], "possible secrets committed:\n" + "\n".join(hits))

    def test_no_credential_files(self):
        bad = [p for p in tracked_files()
               if p.name in {".cookie", ".env", "wallet.dat"} or p.suffix in {".pem", ".key"}]
        self.assertEqual(bad, [])


class EngineCodeHygiene(unittest.TestCase):
    def test_no_dangerous_calls_or_imports(self):
        problems = []
        for path in ENGINE.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                        and node.func.id in BANNED_CALLS:
                    problems.append(f"{path.name}:{node.lineno} {node.func.id}()")
                if isinstance(node, ast.Import):
                    names = {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom):
                    names = {(node.module or "").split(".")[0]}
                else:
                    continue
                for n in (names & BANNED_IMPORTS) - ALLOWED_IMPORTS.get(path, set()):
                    problems.append(f"{path.name}:{node.lineno} import {n}")
        self.assertEqual(problems, [])

    def test_no_shell_true_anywhere(self):
        problems = []
        for path in ENGINE.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.keyword) and node.arg == "shell":
                    problems.append(f"{path.name}:{node.value.lineno} shell=")
        self.assertEqual(problems, [])

    def test_engine_has_no_third_party_dependencies(self):
        import sys

        stdlib = sys.stdlib_module_names
        third_party = set()
        for path in ENGINE.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    mods = [(node.module or "").split(".")[0]]
                else:
                    continue
                third_party |= {m for m in mods if m not in stdlib and m != "sats_engine"}
        self.assertEqual(third_party, set(), "new dependency: review per SECURITY.md first")

    def test_rpc_defaults_to_localhost(self):
        from sats_engine.data.rpc import DEFAULT_RPC_URL

        self.assertTrue(DEFAULT_RPC_URL.startswith("http://127.0.0.1"))


if __name__ == "__main__":
    unittest.main()

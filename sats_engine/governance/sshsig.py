"""Signature verification with OpenSSH `ssh-keygen -Y verify`.

Why ssh-keygen: it is audited, ships everywhere, and supports hardware
security keys (sk-ssh-ed25519@openssh.com), so seat keys can live on a
YubiKey or similar and never touch a disk. This module is the one place
in the engine allowed to run a subprocess (reviewed exception, see
SECURITY.md): fixed argv, no shell, input over stdin, hard timeout.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

NAMESPACE = "psyndica-mandate"
ALLOWED_KEY_TYPES = (
    "ssh-ed25519",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)
_KEY_RE = re.compile(r"^(?P<type>[a-z0-9@.-]+) (?P<b64>[A-Za-z0-9+/]+={0,3})(?: [^\r\n]*)?$")


class SignatureError(RuntimeError):
    pass


def normalize_key(line: str) -> str:
    """Return "type base64" with the comment stripped, or raise.

    Strict parsing stops a crafted key line from smuggling options or extra
    entries into the allowed_signers file.
    """
    m = _KEY_RE.match(line.strip())
    if not m or m["type"] not in ALLOWED_KEY_TYPES:
        raise SignatureError(f"unsupported or malformed public key (allowed: {', '.join(ALLOWED_KEY_TYPES)})")
    return f"{m['type']} {m['b64']}"


class SSHVerifier:
    def __init__(self, binary: str | None = None, timeout: float = 10.0):
        self.binary = binary or shutil.which("ssh-keygen")
        self.timeout = timeout

    def verify(self, message: bytes, signature: bytes, principal: str, pubkeys: list[str]) -> bool:
        if not self.binary:
            # Fail closed: no verifier means no valid votes.
            raise SignatureError("ssh-keygen not found; install OpenSSH (openssh-client) to verify mandates")
        if not re.fullmatch(r"[A-Z]+", principal):
            raise SignatureError("bad principal")
        keys = [normalize_key(k) for k in pubkeys]
        if not keys:
            return False
        with tempfile.TemporaryDirectory() as d:
            signers = os.path.join(d, "allowed_signers")
            sig = os.path.join(d, "vote.sig")
            with open(signers, "w") as f:
                for k in keys:
                    f.write(f'{principal} namespaces="{NAMESPACE}" {k}\n')
            with open(sig, "wb") as f:
                f.write(signature)
            res = subprocess.run(
                [self.binary, "-Y", "verify", "-f", signers, "-I", principal, "-n", NAMESPACE, "-s", sig],
                input=message,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        return res.returncode == 0

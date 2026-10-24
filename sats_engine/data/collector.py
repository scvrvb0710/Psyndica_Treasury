"""Client for the Cloudflare sats collector (see collector/).

The scanner pushes each snapshot out to the Worker; nothing connects in to
the node. Requests are HMAC-signed with a shared secret read from the
SATS_COLLECTOR_SECRET environment variable - never from argv, where it
would show up in `ps` and shell history.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import asdict
from typing import Callable, Optional

from ..models import OnChainSnapshot

SECRET_ENV = "SATS_COLLECTOR_SECRET"
SCHEMA_VERSION = 1
MAX_PENDING = 1000

Opener = Callable[[urllib.request.Request], bytes]


class CollectorError(RuntimeError):
    pass


def load_secret() -> str:
    secret = os.environ.get(SECRET_ENV, "")
    if len(secret) < 32:
        raise CollectorError(f"{SECRET_ENV} must be set to the collector secret (32+ chars)")
    return secret


def sign(secret: str, ts: int, method: str, path_and_query: str, body: str) -> str:
    msg = f"{ts}.{method}.{path_and_query}.{body}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def _default_open(req: urllib.request.Request) -> bytes:
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()


def is_empty(snap: OnChainSnapshot) -> bool:
    return all(v is None for k, v in asdict(snap).items() if k != "ts")


class CollectorClient:
    def __init__(self, base_url: str, secret: str, opener: Optional[Opener] = None,
                 clock: Callable[[], float] = time.time):
        if not base_url.startswith("https://"):
            raise CollectorError("collector URL must be https://")
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self._open = opener or _default_open
        self._clock = clock
        self.pending: deque[dict] = deque(maxlen=MAX_PENDING)

    def _request(self, method: str, path_and_query: str, body: str = "") -> bytes:
        ts = int(self._clock())
        req = urllib.request.Request(
            self.base_url + path_and_query,
            data=body.encode() if method == "POST" else None,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-Sats-Timestamp": str(ts),
                "X-Sats-Signature": sign(self.secret, ts, method, path_and_query, body),
            },
        )
        return self._open(req)

    def push(self, snap: OnChainSnapshot, trace_id: str, source: str) -> int:
        """Queue one snapshot and flush the queue. Returns how many were sent.

        Empty snapshots (node syncing or down) are not sent: the Worker's
        watchdog reports staleness. Network failures keep items queued for the
        next call; the local --save CSV stays the durable record.
        """
        if not is_empty(snap):
            fields = {k: v for k, v in asdict(snap).items() if v is not None}
            self.pending.append(
                {"schema_version": SCHEMA_VERSION, "trace_id": trace_id, "source": source, "snapshot": fields}
            )
        sent = 0
        while self.pending:
            body = json.dumps(self.pending[0], separators=(",", ":"))
            try:
                self._request("POST", "/ingest", body)
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500:
                    # The Worker rejected it (bad data or auth). Retrying won't
                    # help - drop it and surface the reason.
                    self.pending.popleft()
                    raise CollectorError(f"collector rejected snapshot: HTTP {e.code}") from e
                return sent
            except (urllib.error.URLError, OSError):
                return sent
            self.pending.popleft()
            sent += 1
        return sent

    def export_csv(self, source: str = "rpc", since: int = 0) -> str:
        query = urllib.parse.urlencode({"source": source, "since": since})
        return self._request("GET", f"/export?{query}").decode()

"""On-chain Pressure Index (OPI) built from block-explorer data.

The index does not predict direction. Fee spikes, mempool backlogs and
whale-sized transfers mark moments when coins are moving urgently - usually
toward exchanges at local tops (distribution) or in panics at local bottoms
(capitulation). The engine uses heat as a *confirmation and sizing* input:
price structure decides the direction, heat decides how hard to lean.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from ..config import OnChainParams
from ..models import OnChainSnapshot
from .technical import RollingStats


@dataclass(frozen=True)
class PressureReading:
    heat: float  # -1..1; >0 = unusually urgent on-chain activity
    fee_z: Optional[float]
    mempool_z: Optional[float]
    whale_z: Optional[float]
    hash_drawdown: Optional[float]  # 0..1 below rolling max hashrate
    miner_stress: bool

    @property
    def ready(self) -> bool:
        return any(v is not None for v in (self.fee_z, self.mempool_z, self.whale_z))


NEUTRAL = PressureReading(0.0, None, None, None, None, False)


class OnChainPressure:
    def __init__(self, params: OnChainParams):
        self.p = params
        self._fee = RollingStats(params.window)
        self._mempool = RollingStats(params.window)
        self._whale = RollingStats(params.window)
        self._hash = RollingStats(params.window)
        self.last: PressureReading = NEUTRAL

    @staticmethod
    def _log(x: float) -> float:
        # Fees and backlog are heavy-tailed; z-score them in log space.
        return math.log1p(max(x, 0.0))

    def _component(self, stats: RollingStats, raw: Optional[float]) -> Optional[float]:
        if raw is None:
            return None
        x = self._log(raw)
        z = stats.z(x) if stats.count >= max(10, stats.window // 4) else None
        stats.update(x)
        return z

    def update(self, snap: OnChainSnapshot) -> PressureReading:
        fee_z = self._component(self._fee, snap.fee_fast)
        mempool_z = self._component(self._mempool, snap.mempool_vsize)
        whale_z = self._component(self._whale, snap.whale_btc)

        hash_dd = None
        if snap.hashrate is not None:
            self._hash.update(snap.hashrate)
            peak = self._hash.max
            hash_dd = 1.0 - snap.hashrate / peak if peak > 0 else 0.0

        parts = [
            (self.p.w_fee, fee_z),
            (self.p.w_mempool, mempool_z),
            (self.p.w_whale, whale_z),
        ]
        live = [(w, z) for w, z in parts if z is not None]
        if live:
            wsum = sum(w for w, _ in live)
            raw = sum(w * z for w, z in live) / wsum
            # Squash so a z of ~2 reads as strong heat (~0.76) without blowing up.
            heat = math.tanh(raw / 2.0)
        else:
            heat = 0.0

        self.last = PressureReading(
            heat=heat,
            fee_z=fee_z,
            mempool_z=mempool_z,
            whale_z=whale_z,
            hash_drawdown=hash_dd,
            miner_stress=hash_dd is not None and hash_dd >= self.p.hash_stress_dd,
        )
        return self.last

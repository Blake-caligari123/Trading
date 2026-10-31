"""Hard risk layer. Sits between ANY strategy (including AI-written ones) and the broker.

The self-improvement loop only ever writes files under strategies/, so nothing the AI
generates can loosen these limits.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import RiskLimits


@dataclass
class RiskState:
    day_start_equity: float
    halted: bool = False
    halt_reason: str = ""


class RiskManager:
    def __init__(self, limits: RiskLimits, symbols: list[str]):
        self.limits = limits
        self.allowed = set(symbols)
        self.state: RiskState | None = None

    def start_day(self, equity: float) -> None:
        self.state = RiskState(day_start_equity=equity)

    def check_kill_switch(self, equity: float) -> bool:
        """Returns True if trading must halt (daily loss limit breached)."""
        if self.state is None:
            self.start_day(equity)
        s = self.state
        dd = (s.day_start_equity - equity) / s.day_start_equity if s.day_start_equity else 0
        if dd >= self.limits.max_daily_loss_pct and not s.halted:
            s.halted = True
            s.halt_reason = f"daily loss {dd:.2%} >= limit {self.limits.max_daily_loss_pct:.2%}"
        return s.halted

    def sanitize_weights(self, weights: dict) -> tuple[dict[str, float], list[str]]:
        """Clamp strategy output to the hard limits. Returns (clean weights, warnings)."""
        notes: list[str] = []
        clean: dict[str, float] = {}
        if not isinstance(weights, dict):
            return {}, [f"strategy returned {type(weights).__name__}, expected dict -> flat"]
        for sym, w in weights.items():
            if sym not in self.allowed:
                notes.append(f"dropped non-allowlisted symbol {sym!r}")
                continue
            try:
                w = float(w)
            except (TypeError, ValueError):
                notes.append(f"dropped non-numeric weight for {sym}")
                continue
            if not math.isfinite(w):
                notes.append(f"dropped non-finite weight for {sym}")
                continue
            if w < 0 and not self.limits.allow_short:
                w = 0.0
            cap = self.limits.max_position_pct
            if abs(w) > cap:
                notes.append(f"{sym} weight {w:+.2f} clamped to ±{cap:.2f}")
                w = math.copysign(cap, w)
            clean[sym] = w
        gross = sum(abs(w) for w in clean.values())
        if gross > self.limits.max_gross_exposure:
            k = self.limits.max_gross_exposure / gross
            clean = {s: w * k for s, w in clean.items()}
            notes.append(f"gross exposure {gross:.2f} scaled to {self.limits.max_gross_exposure:.2f}")
        return clean, notes

    def clamp_order_qty(self, qty: float, price: float) -> float:
        if price <= 0:
            return 0.0
        max_qty = self.limits.max_order_notional / price
        return min(qty, max_qty)

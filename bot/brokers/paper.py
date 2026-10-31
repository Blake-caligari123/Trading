"""In-process simulated broker. Fills market orders at the feed's last price plus slippage."""
from __future__ import annotations

import time

from .base import Broker, Position


class PaperBroker(Broker):
    name = "paper_sim"

    def __init__(self, price_fn, starting_cash: float = 100_000.0, slippage_bps: float = 2.0, fee_per_share: float = 0.0):
        self.price_fn = price_fn
        self._cash = starting_cash
        self._pos: dict[str, dict] = {}
        self.slippage = slippage_bps / 10_000
        self.fee = fee_per_share
        self.fills: list[dict] = []

    def cash(self) -> float:
        return self._cash

    def positions(self) -> dict[str, Position]:
        return {
            s: Position(s, p["qty"], p["avg"], self.price_fn(s))
            for s, p in self._pos.items()
            if abs(p["qty"]) > 1e-9
        }

    def equity(self) -> float:
        return self._cash + sum(p.market_value for p in self.positions().values())

    def submit_order(self, symbol: str, qty: float, side: str) -> dict:
        if qty <= 0:
            raise ValueError("qty must be positive")
        px = self.price_fn(symbol)
        sign = 1 if side == "buy" else -1
        fill = px * (1 + sign * self.slippage)
        signed = sign * qty
        p = self._pos.setdefault(symbol, {"qty": 0.0, "avg": 0.0})
        new_qty = p["qty"] + signed
        if p["qty"] == 0 or (p["qty"] > 0) == (signed > 0):
            p["avg"] = (p["avg"] * abs(p["qty"]) + fill * qty) / abs(new_qty) if new_qty else 0.0
        elif (p["qty"] > 0) != (new_qty > 0) and abs(new_qty) > 1e-9:
            p["avg"] = fill  # flipped side
        p["qty"] = new_qty
        self._cash -= signed * fill + self.fee * qty
        rec = {"id": f"sim-{len(self.fills)+1}", "symbol": symbol, "side": side, "qty": qty,
               "price": round(fill, 4), "ts": time.time(), "status": "filled"}
        self.fills.append(rec)
        return rec

    def snapshot(self) -> dict:
        return {"cash": self._cash, "pos": self._pos, "fills": self.fills[-500:]}

    def restore(self, snap: dict) -> None:
        self._cash, self._pos, self.fills = snap["cash"], snap["pos"], snap.get("fills", [])

    def close_all(self) -> list[dict]:
        out = []
        for s, p in list(self.positions().items()):
            out.append(self.submit_order(s, abs(p.qty), "sell" if p.qty > 0 else "buy"))
        return out

"""Alpaca Trading API v2 (paper or live)."""
from __future__ import annotations

import httpx

from .base import Broker, Position

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


class AlpacaBroker(Broker):
    def __init__(self, key: str, secret: str, live: bool = False):
        self.live = live
        self.name = "alpaca-LIVE" if live else "alpaca-paper"
        self.client = httpx.Client(
            base_url=LIVE_URL if live else PAPER_URL,
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=20,
        )

    def _get(self, path: str):
        r = self.client.get(path)
        r.raise_for_status()
        return r.json()

    def account(self) -> dict:
        return self._get("/v2/account")

    def equity(self) -> float:
        return float(self.account()["equity"])

    def cash(self) -> float:
        return float(self.account()["cash"])

    def positions(self) -> dict[str, Position]:
        out = {}
        for p in self._get("/v2/positions"):
            sym = p["symbol"]
            out[sym] = Position(sym, float(p["qty"]), float(p["avg_entry_price"]), float(p["current_price"]))
        return out

    def submit_order(self, symbol: str, qty: float, side: str) -> dict:
        body = {"symbol": symbol, "qty": str(round(qty, 6)), "side": side, "type": "market",
                "time_in_force": "gtc" if "/" in symbol else "day"}
        r = self.client.post("/v2/orders", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"Alpaca rejected order {body}: {r.status_code} {r.text}")
        o = r.json()
        return {"id": o["id"], "symbol": symbol, "side": side, "qty": qty, "status": o["status"]}

    def close_all(self) -> list[dict]:
        r = self.client.delete("/v2/positions", params={"cancel_orders": "true"})
        r.raise_for_status()
        return r.json() if r.content else []

    def clock(self) -> dict:
        return self._get("/v2/clock")

    def market_open(self) -> bool:
        try:
            return bool(self._get("/v2/clock")["is_open"])
        except Exception:
            return False

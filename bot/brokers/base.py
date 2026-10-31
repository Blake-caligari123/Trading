from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Position:
    symbol: str
    qty: float
    avg_price: float
    market_price: float

    @property
    def market_value(self) -> float:
        return self.qty * self.market_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.market_price - self.avg_price) * self.qty


class Broker(ABC):
    name = "base"

    @abstractmethod
    def equity(self) -> float: ...

    @abstractmethod
    def cash(self) -> float: ...

    @abstractmethod
    def positions(self) -> dict[str, Position]: ...

    @abstractmethod
    def submit_order(self, symbol: str, qty: float, side: str) -> dict:
        """Market order. side is 'buy' or 'sell'. Returns a dict describing the order/fill."""

    @abstractmethod
    def close_all(self) -> list[dict]: ...

    def market_open(self) -> bool:
        return True

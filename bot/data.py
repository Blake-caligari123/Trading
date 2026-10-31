"""Market data feeds. A bar is a plain dict: {t, o, h, l, c, v}."""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import httpx

Bar = dict


class SyntheticFeed:
    """Offline regime-switching random walk. One call to advance() = one new bar per symbol.

    Lets the whole bot (trading, backtests, evolution) run with zero API keys.
    """

    def __init__(self, symbols: list[str], seed: int = 7, warmup: int = 600):
        self.rng = random.Random(seed)
        self.symbols = symbols
        self.bars: dict[str, list[Bar]] = {s: [] for s in symbols}
        self._state = {s: {"p": self.rng.uniform(50, 500), "drift": 0.0, "vol": 0.002} for s in symbols}
        self._t = 0
        for _ in range(warmup):
            self.advance()

    def advance(self) -> None:
        self._t += 1
        market = self.rng.gauss(0, 0.0015)
        for s in self.symbols:
            st = self._state[s]
            if self.rng.random() < 0.01:  # regime change
                st["drift"] = self.rng.gauss(0, 0.0006)
                st["vol"] = self.rng.uniform(0.001, 0.004)
            r = st["drift"] + 0.6 * market + self.rng.gauss(0, st["vol"])
            o = st["p"]
            c = max(0.01, o * math.exp(r))
            h = max(o, c) * (1 + abs(self.rng.gauss(0, st["vol"] / 2)))
            l = min(o, c) * (1 - abs(self.rng.gauss(0, st["vol"] / 2)))
            st["p"] = c
            self.bars[s].append({"t": self._t, "o": o, "h": h, "l": l, "c": c, "v": self.rng.randint(1_000, 50_000)})
            if len(self.bars[s]) > 5000:
                del self.bars[s][:1000]

    def history(self, lookback: int) -> dict[str, list[Bar]]:
        return {s: b[-lookback:] for s, b in self.bars.items()}

    def last_price(self, symbol: str) -> float:
        return self.bars[symbol][-1]["c"]


class AlpacaFeed:
    """Historical/latest bars from Alpaca's market data API (stocks; crypto if symbol contains '/')."""

    BASE = "https://data.alpaca.markets"

    def __init__(self, key: str, secret: str, symbols: list[str], timeframe: str = "5Min"):
        self.symbols = symbols
        self.timeframe = timeframe
        self.client = httpx.Client(
            base_url=self.BASE,
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=20,
        )
        self._cache: dict[str, list[Bar]] = {}

    def advance(self) -> None:
        self._cache = self._fetch(lookback_days=10)

    def _fetch(self, lookback_days: int) -> dict[str, list[Bar]]:
        start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        out: dict[str, list[Bar]] = {s: [] for s in self.symbols}
        stocks = [s for s in self.symbols if "/" not in s]
        crypto = [s for s in self.symbols if "/" in s]
        for syms, path in ((stocks, "/v2/stocks/bars"), (crypto, "/v1beta3/crypto/us/bars")):
            if not syms:
                continue
            params = {"symbols": ",".join(syms), "timeframe": self.timeframe, "start": start, "limit": 10000}
            if path.startswith("/v2"):
                params["feed"] = "iex"
            token = None
            while True:
                if token:
                    params["page_token"] = token
                r = self.client.get(path, params=params)
                r.raise_for_status()
                j = r.json()
                for sym, bars in (j.get("bars") or {}).items():
                    out[sym].extend({"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]} for b in bars)
                token = j.get("next_page_token")
                if not token:
                    break
        return out

    def history(self, lookback: int) -> dict[str, list[Bar]]:
        if not self._cache:
            self.advance()
        return {s: b[-lookback:] for s, b in self._cache.items()}

    def last_price(self, symbol: str) -> float:
        bars = self.history(1).get(symbol) or []
        if not bars:
            raise RuntimeError(f"No price for {symbol}")
        return bars[-1]["c"]

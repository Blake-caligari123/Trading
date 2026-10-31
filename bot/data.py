"""Market data feeds. A bar is a plain dict: {t, o, h, l, c, v}."""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import httpx

from .analysis import TIMEFRAMES, resample

Bar = dict


class SyntheticFeed:
    """Offline regime-switching random walk. One call to advance() = one new bar per symbol.

    Lets the whole bot (trading, backtests, evolution) run with zero API keys.
    """

    def __init__(self, symbols: list[str], seed: int = 7, warmup: int = 4000):
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

    def get_bars(self, symbol: str, timeframe: str = "5Min", n: int = 100) -> list[Bar]:
        factor = TIMEFRAMES.get(timeframe)
        if factor is None:
            raise ValueError(f"timeframe must be one of {list(TIMEFRAMES)}")
        return resample(self.bars[symbol][-(n * factor + factor):], factor)[-n:]

    def quote(self, symbol: str) -> dict:
        last = self.last_price(symbol)
        day = self.bars[symbol][-78:]
        return {"symbol": symbol, "last": round(last, 4), "bid": round(last * 0.9999, 4), "ask": round(last * 1.0001, 4),
                "day_open": round(day[0]["o"], 4), "day_high": round(max(b["h"] for b in day), 4),
                "day_low": round(min(b["l"] for b in day), 4), "day_change": round(last / day[0]["o"] - 1, 5),
                "source": "simulated market"}

    def news(self, symbol: str, n: int = 10) -> list[dict]:
        return [{"note": "simulated market: there is no news. Use your own web/X search for real-world context."}]

    def clock(self) -> dict:
        return {"is_open": True, "note": "simulated market trades 24/7; 78 bars = 1 trading day"}

    def snapshot(self, keep: int = 5000) -> dict:
        v, internal, gauss = self.rng.getstate()
        return {"t": self._t, "state": self._state, "bars": {s: b[-keep:] for s, b in self.bars.items()},
                "rng": [v, list(internal), gauss]}

    def restore(self, snap: dict) -> bool:
        """Continue the same simulated market after a restart. Ignored if the symbol list changed."""
        if set(snap.get("bars", {})) != set(self.symbols):
            return False
        self._t, self._state, self.bars = snap["t"], snap["state"], snap["bars"]
        v, internal, gauss = snap["rng"]
        self.rng.setstate((v, tuple(internal), gauss))
        return True


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

    _DAYS_FOR = {"1Min": 3, "5Min": 10, "15Min": 20, "30Min": 30, "1Hour": 45, "4Hour": 120, "1Day": 400}

    def get_bars(self, symbol: str, timeframe: str = "5Min", n: int = 100) -> list[Bar]:
        if timeframe not in self._DAYS_FOR:
            raise ValueError(f"timeframe must be one of {list(self._DAYS_FOR)}")
        crypto = "/" in symbol
        start = (datetime.now(timezone.utc) - timedelta(days=self._DAYS_FOR[timeframe])).isoformat()
        params = {"symbols": symbol, "timeframe": timeframe, "start": start, "limit": 10000}
        if not crypto:
            params["feed"] = "iex"
        r = self.client.get("/v1beta3/crypto/us/bars" if crypto else "/v2/stocks/bars", params=params)
        r.raise_for_status()
        bars = (r.json().get("bars") or {}).get(symbol, [])
        return [{"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]} for b in bars][-n:]

    def quote(self, symbol: str) -> dict:
        crypto = "/" in symbol
        path = "/v1beta3/crypto/us/snapshots" if crypto else "/v2/stocks/snapshots"
        params = {"symbols": symbol} | ({} if crypto else {"feed": "iex"})
        r = self.client.get(path, params=params)
        r.raise_for_status()
        j = r.json()
        snap = (j.get("snapshots") or j).get(symbol, {})
        q, d, t = snap.get("latestQuote", {}), snap.get("dailyBar", {}), snap.get("latestTrade", {})
        last = t.get("p") or d.get("c")
        return {"symbol": symbol, "last": last, "bid": q.get("bp"), "ask": q.get("ap"),
                "day_open": d.get("o"), "day_high": d.get("h"), "day_low": d.get("l"),
                "day_change": round(last / d["o"] - 1, 5) if last and d.get("o") else None,
                "prev_close": (snap.get("prevDailyBar") or {}).get("c"), "source": "alpaca iex"}

    def news(self, symbol: str, n: int = 10) -> list[dict]:
        r = self.client.get("/v1beta1/news", params={"symbols": symbol.replace("/", ""), "limit": max(1, min(n, 50))})
        r.raise_for_status()
        return [{"time": a.get("created_at"), "headline": a.get("headline"), "summary": (a.get("summary") or "")[:400],
                 "source": a.get("source"), "url": a.get("url")} for a in r.json().get("news", [])]

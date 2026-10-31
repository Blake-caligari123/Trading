"""Technical analysis for the AI: indicators, multi-timeframe bars, market overview. Pure Python."""
from __future__ import annotations

import math

# How many base bars (BAR_TIMEFRAME, default 5Min) make one bar of each timeframe, for the synthetic feed.
TIMEFRAMES = {"5Min": 1, "15Min": 3, "30Min": 6, "1Hour": 12, "4Hour": 48, "1Day": 78}


def resample(bars: list[dict], factor: int) -> list[dict]:
    """Aggregate consecutive bars into bars `factor` times longer (aligned to the end)."""
    if factor <= 1:
        return list(bars)
    out = []
    start = len(bars) % factor
    for i in range(start, len(bars), factor):
        chunk = bars[i:i + factor]
        if len(chunk) < factor:
            break
        out.append({"t": chunk[-1]["t"], "o": chunk[0]["o"], "h": max(b["h"] for b in chunk),
                    "l": min(b["l"] for b in chunk), "c": chunk[-1]["c"], "v": sum(b["v"] for b in chunk)})
    return out


def sma(xs: list[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def ema_series(xs: list[float], n: int) -> list[float]:
    if not xs:
        return []
    k = 2 / (n + 1)
    out = [xs[0]]
    for x in xs[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):  # Wilder's smoothing
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def atr(bars: list[dict], n: int = 14) -> float | None:
    if len(bars) < n + 1:
        return None
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(bars, bars[1:])]
    return sum(trs[-n:]) / n


def _r(x, d=4):
    return None if x is None else round(x, d)


def indicators(bars: list[dict]) -> dict:
    """A compact, LLM-friendly technical snapshot of one symbol on one timeframe."""
    if len(bars) < 2:
        return {"error": "not enough bars"}
    closes = [b["c"] for b in bars]
    last = closes[-1]
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    e12, e26 = ema_series(closes, 12), ema_series(closes, 26)
    macd_line = [a - b for a, b in zip(e12, e26)]
    signal = ema_series(macd_line, 9)
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    vol = math.sqrt(sum(r * r for r in rets[-20:]) / max(1, len(rets[-20:]))) if rets else None
    bb_mid = s20
    bb_sd = math.sqrt(sum((c - bb_mid) ** 2 for c in closes[-20:]) / 20) if bb_mid else None
    a14 = atr(bars, 14)
    window = bars[-50:]
    vols = [b["v"] for b in bars[-21:-1]]
    trend = "unknown"
    if s20 and s50:
        if last > s20 > s50:
            trend = "uptrend"
        elif last < s20 < s50:
            trend = "downtrend"
        else:
            trend = "sideways/mixed"
    r14 = rsi(closes, 14)

    def chg(n):
        return _r(closes[-1] / closes[-1 - n] - 1, 5) if len(closes) > n else None

    return {
        "last": _r(last), "bars_used": len(bars), "trend": trend,
        "change": {"1_bar": chg(1), "5_bars": chg(5), "20_bars": chg(20), "50_bars": chg(50)},
        "sma": {"20": _r(s20), "50": _r(s50), "200": _r(s200)},
        "ema": {"12": _r(e12[-1]), "26": _r(e26[-1])},
        "rsi_14": _r(r14, 2),
        "rsi_state": None if r14 is None else ("overbought" if r14 > 70 else "oversold" if r14 < 30 else "neutral"),
        "macd": {"line": _r(macd_line[-1], 5), "signal": _r(signal[-1], 5), "histogram": _r(macd_line[-1] - signal[-1], 5)},
        "bollinger_20_2": None if not bb_sd else {"upper": _r(bb_mid + 2 * bb_sd), "mid": _r(bb_mid),
                                                  "lower": _r(bb_mid - 2 * bb_sd),
                                                  "pct_b": _r((last - (bb_mid - 2 * bb_sd)) / (4 * bb_sd), 3) if bb_sd else None},
        "atr_14": _r(a14), "atr_pct": _r(a14 / last, 5) if a14 else None,
        "volatility_per_bar_20": _r(vol, 5),
        "support_50": _r(min(b["l"] for b in window)), "resistance_50": _r(max(b["h"] for b in window)),
        "volume_vs_20_avg": _r(bars[-1]["v"] / (sum(vols) / len(vols)), 2) if vols and sum(vols) else None,
        "suggested_stop_long_2atr": _r(last - 2 * a14) if a14 else None,
        "suggested_stop_short_2atr": _r(last + 2 * a14) if a14 else None,
    }

"""Strategy v1 (seed): trend-following momentum with volatility scaling.

THIS FILE IS REWRITTEN BY THE BOT. Contract:
  generate_signals(history, positions, params) -> {symbol: target_weight}
    history:   {symbol: [ {t,o,h,l,c,v}, ... ]}  oldest -> newest, up to 200 bars
    positions: {symbol: current_weight}          fraction of equity currently held
    params:    copy of PARAMS
  Weights are fractions of equity. Risk layer clamps to MAX_POSITION_PCT / gross / no-short.
  Only `math` and `statistics` may be imported.
"""
import math

PARAMS = {
    "fast": 12,
    "slow": 48,
    "vol_window": 30,
    "target_vol": 0.002,   # per-bar volatility target per position
    "max_weight": 0.2,
    "entry_threshold": 0.0015,
}


def _sma(xs, n):
    n = max(1, min(n, len(xs)))
    return sum(xs[-n:]) / n


def _stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def generate_signals(history, positions, params):
    out = {}
    fast, slow = int(params["fast"]), int(params["slow"])
    for sym, bars in history.items():
        closes = [b["c"] for b in bars]
        if len(closes) < slow + 2:
            out[sym] = 0.0
            continue
        trend = _sma(closes, fast) / _sma(closes, slow) - 1
        rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - int(params["vol_window"]), len(closes))]
        vol = _stdev(rets) or 1e-6
        if trend > params["entry_threshold"]:
            w = min(params["max_weight"], params["target_vol"] / vol * params["max_weight"])
        else:
            w = 0.0
        out[sym] = round(w, 4)
    return out

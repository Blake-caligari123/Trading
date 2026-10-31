"""Bar-by-bar backtester that drives the exact same strategy interface as live trading.

Also usable as a subprocess entry point (used by the evolver to isolate untrusted candidates):
    python -I -c "...; from bot.backtest import cli; cli()" <strategy.py> <bars.json>
"""
from __future__ import annotations

import json
import math
import sys

from .sandbox import load_strategy

LOOKBACK = 200
REBALANCE_BAND = 0.01  # ignore target changes smaller than 1% of equity (shared with live engine)


def run_backtest(ns: dict, bars: dict[str, list[dict]], start: int = LOOKBACK, end: int | None = None,
                 cost_bps: float = 3.0, max_pos: float = 0.2, max_gross: float = 1.0,
                 allow_short: bool = False, rebalance_every: int = 1) -> dict:
    gen, params = ns["generate_signals"], dict(ns.get("PARAMS", {}))
    syms = list(bars)
    n = min(len(b) for b in bars.values())
    end = n if end is None else min(end, n)
    equity, cash = 1.0, 1.0
    units = {s: 0.0 for s in syms}
    curve, turnover, trades = [], 0.0, 0
    for i in range(start, end):
        px = {s: bars[s][i - 1]["c"] for s in syms}
        equity = cash + sum(units[s] * px[s] for s in syms)
        if (i - start) % rebalance_every == 0:
            hist = {s: bars[s][max(0, i - LOOKBACK):i] for s in syms}
            weights_now = {s: units[s] * px[s] / equity for s in syms} if equity > 0 else {}
            try:
                w = gen(hist, weights_now, params) or {}
            except Exception as e:  # a crashing strategy is a failing strategy
                return {"error": f"{type(e).__name__}: {e}", "score": -99}
            w = _clamp(w, syms, max_pos, max_gross, allow_short)
            for s in syms:
                target_units = w.get(s, 0.0) * equity / px[s]
                d = target_units - units[s]
                if abs(d * px[s]) > REBALANCE_BAND * equity or (target_units == 0 and units[s] != 0):
                    cost = abs(d * px[s]) * cost_bps / 10_000
                    cash -= d * px[s] + cost
                    turnover += abs(d * px[s])
                    units[s] = target_units
                    trades += 1
        # mark to the close of bar i
        equity = cash + sum(units[s] * bars[s][i]["c"] for s in syms)
        curve.append(equity)
    return _metrics(curve, turnover, trades)


def _clamp(w, syms, max_pos, max_gross, allow_short):
    out = {}
    if not isinstance(w, dict):
        return out
    for s, v in w.items():
        if s not in syms:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            continue
        if v < 0 and not allow_short:
            v = 0.0
        out[s] = max(-max_pos, min(max_pos, v))
    g = sum(abs(v) for v in out.values())
    if g > max_gross:
        out = {s: v * max_gross / g for s, v in out.items()}
    return out


def _metrics(curve: list[float], turnover: float, trades: int) -> dict:
    if len(curve) < 3:
        return {"error": "not enough bars", "score": -99}
    rets = [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve))]
    mu = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) or 1e-12
    sharpe = mu / sd * math.sqrt(252 * 78)  # annualized assuming 5-min bars, 78/day
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
    total = curve[-1] / curve[0] - 1
    # score: risk-adjusted return, penalize deep drawdowns and doing nothing
    score = sharpe - 5 * max(0.0, mdd - 0.10) - (0.5 if trades == 0 else 0.0)
    return {"total_return": round(total, 5), "sharpe": round(sharpe, 3), "max_drawdown": round(mdd, 4),
            "trades": trades, "turnover": round(turnover, 2), "bars": len(curve), "score": round(score, 3)}


def walk_forward(ns: dict, bars: dict[str, list[dict]], holdout_frac: float = 0.3) -> dict:
    """In-sample / out-of-sample split. The out-of-sample score is what decides promotion."""
    n = min(len(b) for b in bars.values())
    split = int(n * (1 - holdout_frac))
    return {"in_sample": run_backtest(ns, bars, end=split),
            "holdout": run_backtest(ns, bars, start=max(LOOKBACK, split))}


def cli() -> None:
    strat_path, bars_path = sys.argv[1], sys.argv[2]
    ns = load_strategy(open(strat_path).read(), strat_path)
    bars = json.load(open(bars_path))
    print(json.dumps(walk_forward(ns, bars)))

"""MCP server: lets ANY AI tool (Grok, Claude, ChatGPT, Cursor, …) research, trade and steer the bot.

Served over Streamable HTTP at  http(s)://<bot>/mcp  and protected by DASHBOARD_TOKEN
(Bearer header, or ?token= for clients that only take a URL).

The AI can: research (quotes, multi-timeframe bars, indicators, news, overview, strategy signals),
trade directly (buy / sell / close with stop-loss + take-profit, dry-run previews), manage the automatic
strategy (backtest, submit, evolve, rollback) and pause / flatten.
It cannot: change risk limits, trade outside the symbol universe, or override an operator's pause.
Every order passes the hard risk layer; risk-reducing orders are always allowed.
"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from .evolve import ACTIVE
from .sandbox import ALLOWED_IMPORTS, StrategyRejected

if TYPE_CHECKING:
    from .engine import Engine

INSTRUCTIONS = f"""You are connected to a live automated trading bot. You can research the market and trade directly.

Recommended workflow before every trade:
 1. get_account          -> equity, cash, positions, exposure, limits, how much you may still buy per symbol
 2. get_market_overview  -> every symbol: price, day change, trend, RSI, your position, the strategy's signal
 3. analyze_symbol       -> indicators on 2+ timeframes (e.g. 15Min for entry, 1Hour/1Day for trend)
 4. get_news / your own web+X search for real-world context (the simulated market has no news)
 5. buy / sell with dry_run=true first, then for real, ALWAYS with a stop_loss (analyze_symbol suggests a 2*ATR stop)
    and a clear `reason`. Size with percent_equity or notional; risk ~0.5-1% of equity per trade.
 6. get_trade_journal afterwards to review what worked.

How trading works here
- Two traders share the account: the automatic STRATEGY and the DESK (you / the operator).
  A symbol you trade is taken over by the desk; the strategy stops touching it until you call release_symbol.
  set_trading_mode("manual") makes the desk the only trader.
- Hard limits (cannot be changed): max position % of equity, max gross exposure, max order size, symbol universe,
  shorting only if allowed, desk orders per hour, and a daily-loss kill switch.
  Rejected orders say why and give the max size allowed. Closing / reducing positions is always allowed.
- Stops and targets are checked every bar by the bot and executed as market orders.
- If the operator paused trading you cannot resume it; you may still reduce risk.

Strategy tools: the automatic strategy is Python: PARAMS dict + generate_signals(history, positions, params)
-> {{symbol: weight}}. Only imports {sorted(ALLOWED_IMPORTS)}; no I/O, classes, eval/exec/getattr, underscore
attributes. submit_strategy only goes live if it beats the current one on held-out data."""


def _load_sdk():
    try:  # mcp SDK 2.x
        from mcp.server.mcpserver import MCPServer
        return MCPServer, 2
    except ImportError:
        from mcp.server.fastmcp import FastMCP  # mcp SDK 1.x
        return FastMCP, 1


def _j(x) -> str:
    return json.dumps(x, default=str)


def build_mcp(engine: "Engine"):
    Server, major = _load_sdk()
    mcp = Server("trading-bot", instructions=INSTRUCTIONS)
    desk = engine.desk

    async def run(fn, *a, **kw):
        return await asyncio.to_thread(fn, *a, **kw)

    # ================= account & state =================
    @mcp.tool()
    async def get_account() -> str:
        """Account snapshot: equity, cash, P&L today, gross/net exposure, every position (qty, avg price, weight,
        unrealized P&L, stop/target, whether the desk or the strategy manages it), trading mode, pause/kill-switch
        state, the hard limits, desk orders left this hour, and the max new long notional per symbol."""
        return _j(await run(desk.account))

    @mcp.tool()
    async def get_status() -> str:
        """Short engine status: running/paused/halted, equity, P&L, strategy version, mode."""
        return _j(engine.status_line() | {"summary": engine.live_summary()})

    @mcp.tool()
    async def get_trades(n: int = 25) -> str:
        """The most recent n fills from both the strategy and the desk."""
        return _j(engine.trades[-max(1, min(n, 500)):])

    @mcp.tool()
    async def get_trade_journal(n: int = 30) -> str:
        """Desk orders (yours, the operator's and automatic stop/target exits) with their reason, price,
        stop/target and realized P&L. Use it to review your own decisions."""
        return _j(desk.journal[-max(1, min(n, 500)):])

    @mcp.tool()
    async def get_log(n: int = 50, level: str = "") -> str:
        """Recent engine log events. Optional level filter: info, trade, ai, evolve, warn, error, risk."""
        evs = [e for e in engine.bus.history if not level or e["level"] == level]
        return _j(evs[-max(1, min(n, 500)):])

    @mcp.tool()
    async def get_risk_limits() -> str:
        """The hard risk limits and the tradable symbol universe (read-only)."""
        return _j(vars(engine.cfg.risk) | {"symbols": engine.cfg.symbols})

    # ================= market research =================
    @mcp.tool()
    async def get_market_overview() -> str:
        """One row per tradable symbol: last price, day change, trend, RSI(14), ATR %, your current weight,
        the automatic strategy's target weight, and who manages the symbol. Plus the market clock."""
        return _j(await run(engine.market_overview))

    @mcp.tool()
    async def get_quote(symbol: str) -> str:
        """Latest price, bid/ask, day open/high/low and day change for one symbol."""
        return _j(await run(engine.feed.quote, symbol.upper()))

    @mcp.tool()
    async def get_bars(symbol: str, timeframe: str = "5Min", n: int = 100) -> str:
        """OHLCV candles, oldest first. timeframe: 5Min, 15Min, 30Min, 1Hour, 4Hour, 1Day. n <= 500."""
        sym = symbol.upper()
        if sym not in engine.cfg.symbols:
            return f"unknown symbol; universe is {engine.cfg.symbols}"
        return _j(await run(engine.feed.get_bars, sym, timeframe, max(1, min(n, 500))))

    @mcp.tool()
    async def analyze_symbol(symbol: str, timeframe: str = "5Min") -> str:
        """Technical snapshot: trend, % changes, SMA 20/50/200, EMA 12/26, RSI(14), MACD, Bollinger bands, ATR(14),
        volatility, 50-bar support/resistance, volume vs average, and suggested 2*ATR stop levels.
        Check at least two timeframes before trading."""
        return _j(await run(engine.analyze, symbol, timeframe))

    @mcp.tool()
    async def get_news(symbol: str, n: int = 10) -> str:
        """Recent news headlines for a symbol (real brokers only; the simulated market has none)."""
        try:
            return _j(await run(engine.feed.news, symbol.upper(), n))
        except Exception as e:
            return f"news unavailable: {e}"

    @mcp.tool()
    async def get_market_clock() -> str:
        """Whether the market is open now, and the next open/close times when the broker provides them."""
        return _j(await run(engine.clock))

    @mcp.tool()
    async def get_strategy_signals() -> str:
        """What the automatic strategy wants to hold right now (target weights) vs current weights."""
        return _j(await run(engine.strategy_signals))

    # ================= direct trading =================
    @mcp.tool()
    async def buy(symbol: str, reason: str, qty: float | None = None, notional: float | None = None,
                  percent_equity: float | None = None, stop_loss: float | None = None,
                  take_profit: float | None = None, dry_run: bool = False) -> str:
        """Market BUY (opens/adds to a long, or covers a short). Give exactly ONE size: qty (units),
        notional (account currency) or percent_equity (e.g. 5 = 5% of equity). Strongly recommended: stop_loss
        (below price) and take_profit (above price). `reason` is required and logged. dry_run=true runs every
        check and returns the projected position without sending an order. Returns status filled / rejected
        (with the reason and max allowed size) / preview."""
        return _j(await run(desk.order, symbol, "buy", qty, notional, percent_equity, stop_loss, take_profit,
                            reason, "mcp-ai", dry_run))

    @mcp.tool()
    async def sell(symbol: str, reason: str, qty: float | None = None, notional: float | None = None,
                   percent_equity: float | None = None, stop_loss: float | None = None,
                   take_profit: float | None = None, dry_run: bool = False) -> str:
        """Market SELL (reduces/closes a long, or opens/adds to a short if shorting is allowed). Same sizing and
        dry_run rules as buy. For a short: stop_loss above price, take_profit below. `reason` is required."""
        return _j(await run(desk.order, symbol, "sell", qty, notional, percent_equity, stop_loss, take_profit,
                            reason, "mcp-ai", dry_run))

    @mcp.tool()
    async def close_position(symbol: str, reason: str, percent: float = 100) -> str:
        """Close all (percent=100) or part of a position at market. Always allowed, even when paused or halted."""
        return _j(await run(desk.close, symbol, percent, reason, "mcp-ai"))

    @mcp.tool()
    async def set_exits(symbol: str, stop_loss: float | None = None, take_profit: float | None = None) -> str:
        """Set or move the stop-loss / take-profit of an open position (pass null to remove one).
        The symbol is taken over by the desk if it wasn't already."""
        return _j(await run(desk.set_exits, symbol, stop_loss, take_profit, "mcp-ai"))

    @mcp.tool()
    async def release_symbol(symbol: str) -> str:
        """Hand a desk-controlled symbol back to the automatic strategy (it may then resize the position)."""
        return _j(desk.release(symbol))

    @mcp.tool()
    async def set_trading_mode(mode: str) -> str:
        """'auto': the strategy trades every symbol not taken over by the desk. 'manual': the strategy stops
        trading entirely and only desk orders (you / operator) and stop/target exits happen."""
        return _j(desk.set_mode(mode, "mcp-ai"))

    @mcp.tool()
    async def pause_trading() -> str:
        """Stop opening new risk (strategy and desk). Stops/targets and risk-reducing orders still work."""
        out = engine.pause("mcp-ai")
        engine.persist()
        return out

    @mcp.tool()
    async def resume_trading() -> str:
        """Resume trading after an AI pause. Refused if the operator paused it."""
        out = engine.resume("mcp-ai")
        engine.persist()
        return out

    @mcp.tool()
    async def flatten_positions() -> str:
        """EMERGENCY: close ALL positions at market and pause trading."""
        return await engine.command("flatten")

    # ================= automatic strategy =================
    @mcp.tool()
    async def get_strategy() -> str:
        """Source code of the automatic strategy and its version number."""
        return f"# version v{engine.evolver.current_version}\n" + ACTIVE.read_text()

    @mcp.tool()
    async def get_versions(n: int = 20) -> str:
        """Journal of strategy versions: author, holdout metrics, promoted or not, and why rejected."""
        rows = [{k: e.get(k) for k in ("version", "author", "promoted", "is_active", "reason", "rationale", "metrics")}
                for e in engine.evolver.journal[-max(1, min(n, 200)):]]
        return _j(rows)

    @mcp.tool()
    async def backtest_strategy(source: str) -> str:
        """Walk-forward backtest a full strategy file WITHOUT activating it (in-sample + holdout metrics)."""
        try:
            return _j(await engine.backtest_source(source))
        except StrategyRejected as e:
            return f"rejected by sandbox: {e}"

    @mcp.tool()
    async def submit_strategy(source: str, rationale: str, author: str = "external-ai") -> str:
        """Propose a replacement strategy file. Goes live ONLY if it passes the sandbox and beats the current
        strategy's out-of-sample score."""
        return _j(await engine.submit_strategy(source, rationale, f"mcp:{author}"))

    @mcp.tool()
    async def run_evolution() -> str:
        """Start a self-improvement cycle using the bot's built-in AI / parameter search (background)."""
        return await engine.command("evolve")

    @mcp.tool()
    async def rollback_strategy(version: int) -> str:
        """Revert the automatic strategy to an earlier version from get_versions."""
        return await engine.command(f"rollback {int(version)}")

    if major == 2:
        from mcp.server.transport_security import TransportSecuritySettings
        # Host/Origin checks are done by our own auth middleware (works behind tunnels/proxies too).
        app = mcp.streamable_http_app(streamable_http_path="/mcp", host=engine.cfg.host,
                                      transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    else:
        mcp.settings.streamable_http_path = "/mcp"
        try:
            from mcp.server.transport_security import TransportSecuritySettings
            mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
        except ImportError:
            pass
        app = mcp.streamable_http_app()
    return mcp, app

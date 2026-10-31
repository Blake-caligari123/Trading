"""MCP server: lets ANY AI tool (Claude, ChatGPT, Cursor, Grok, Gemini CLI, …) monitor and steer the bot.

Served over Streamable HTTP at  http(s)://<bot>/mcp  and protected by DASHBOARD_TOKEN (Bearer).

What an outside AI can do:   read everything, pause/resume/flatten, trigger evolution, roll back,
                             backtest ideas, and SUBMIT strategy code.
What it cannot do:           place arbitrary orders, change risk limits, or bypass the sandbox +
                             out-of-sample gate. A submitted strategy is promoted only if it beats
                             the incumbent on held-out data.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .evolve import ACTIVE
from .sandbox import ALLOWED_IMPORTS, StrategyRejected

if TYPE_CHECKING:
    from .engine import Engine

INSTRUCTIONS = f"""This server controls a live automated trading bot.
Typical workflow: get_status -> get_strategy -> get_versions (see what was tried and why it was rejected)
-> backtest_strategy(your idea) -> submit_strategy(your best idea).
Strategy contract: define PARAMS (dict) and generate_signals(history, positions, params) -> {{symbol: weight}}.
history = {{symbol: [{{t,o,h,l,c,v}}, ...]}} oldest->newest (<=200 bars); positions = {{symbol: current weight}}.
Weights are fractions of equity; the risk layer caps them. Only imports allowed: {sorted(ALLOWED_IMPORTS)}.
No I/O, classes, eval/exec/getattr, or underscore attribute access. ~3 bps cost per trade, so avoid churn.
Promotion requires beating the current strategy on an out-of-sample holdout."""


def _load_sdk():
    try:  # mcp SDK 2.x
        from mcp.server.mcpserver import MCPServer
        return MCPServer, 2
    except ImportError:
        from mcp.server.fastmcp import FastMCP  # mcp SDK 1.x
        return FastMCP, 1


def build_mcp(engine: "Engine"):
    Server, major = _load_sdk()
    mcp = Server("trading-bot", instructions=INSTRUCTIONS)

    @mcp.tool()
    async def get_status() -> str:
        """Equity, P&L today and since start, open positions, engine state, active strategy version."""
        return json.dumps(engine.status_line() | {"summary": engine.live_summary()}, default=str)

    @mcp.tool()
    async def get_positions() -> str:
        """Open positions with quantity, average price, last price and unrealized P&L."""
        return await engine.command("positions")

    @mcp.tool()
    async def get_trades(n: int = 25) -> str:
        """The most recent n fills."""
        return json.dumps(engine.trades[-max(1, min(n, 500)):], default=str)

    @mcp.tool()
    async def get_log(n: int = 50, level: str = "") -> str:
        """Recent engine log events. Optional level filter: info, trade, ai, evolve, warn, error, risk."""
        evs = [e for e in engine.bus.history if not level or e["level"] == level]
        return json.dumps(evs[-max(1, min(n, 500)):], default=str)

    @mcp.tool()
    async def get_market_bars(symbol: str, n: int = 100) -> str:
        """Recent OHLCV bars for one of the bot's symbols."""
        sym = symbol.upper()
        if sym not in engine.cfg.symbols:
            return f"unknown symbol; trading universe is {engine.cfg.symbols}"
        return json.dumps(engine.feed.history(max(1, min(n, 200))).get(sym, []))

    @mcp.tool()
    async def get_strategy() -> str:
        """Source code of the strategy currently trading, plus its version number."""
        return f"# version v{engine.evolver.current_version}\n" + ACTIVE.read_text()

    @mcp.tool()
    async def get_versions(n: int = 20) -> str:
        """Journal of strategy versions: who wrote each, holdout metrics, whether promoted, and why rejected."""
        rows = [{k: e.get(k) for k in ("version", "author", "promoted", "is_active", "reason", "rationale", "metrics")}
                for e in engine.evolver.journal[-max(1, min(n, 200)):]]
        return json.dumps(rows, default=str)

    @mcp.tool()
    async def get_risk_limits() -> str:
        """The hard risk limits (read-only; no tool can change them)."""
        return json.dumps(vars(engine.cfg.risk) | {"symbols": engine.cfg.symbols})

    @mcp.tool()
    async def backtest_strategy(source: str) -> str:
        """Walk-forward backtest a full strategy file WITHOUT activating it. Returns in-sample and holdout metrics."""
        try:
            return json.dumps(await engine.backtest_source(source))
        except StrategyRejected as e:
            return f"rejected by sandbox: {e}"

    @mcp.tool()
    async def submit_strategy(source: str, rationale: str, author: str = "external-ai") -> str:
        """Propose a full replacement strategy file. It goes live ONLY if it passes the sandbox and beats the
        current strategy's out-of-sample score. Returns promoted/kept with the reason."""
        return json.dumps(await engine.submit_strategy(source, rationale, f"mcp:{author}"), default=str)

    @mcp.tool()
    async def run_evolution() -> str:
        """Start a self-improvement cycle using the bot's built-in AI / parameter search (runs in background)."""
        return await engine.command("evolve")

    @mcp.tool()
    async def rollback_strategy(version: int) -> str:
        """Revert to an earlier strategy version from get_versions."""
        return await engine.command(f"rollback {int(version)}")

    @mcp.tool()
    async def pause_trading() -> str:
        """Stop placing orders (positions are kept; data and evolution keep running)."""
        return await engine.command("pause")

    @mcp.tool()
    async def resume_trading() -> str:
        """Resume placing orders."""
        return await engine.command("resume")

    @mcp.tool()
    async def flatten_positions() -> str:
        """Close ALL positions and pause trading. Use when something looks wrong."""
        return await engine.command("flatten")

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

"""Trading engine: data -> strategy -> risk -> orders, plus scheduling of self-improvement."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone

from .backtest import LOOKBACK, REBALANCE_BAND
from .brain import Brain
from .brokers import AlpacaBroker, Broker, PaperBroker
from .config import DATA_DIR, Config
from .data import AlpacaFeed, SyntheticFeed
from .events import EventBus
from .evolve import ACTIVE, VERSIONS, Evolver
from .risk import RiskManager
from .sandbox import StrategyRejected, load_strategy

SIM_BARS_PER_DAY = 78
ASK_SYSTEM = ("You are the AI co-pilot of an automated trading bot, answering its operator in a terminal. "
              "Be concise and concrete. Use tools to look at the bot's live state before answering. "
              "You cannot place trades or change risk limits; the operator can run `evolve` to trigger "
              "a strategy rewrite.")

HELP = """commands:
  status                 account, P&L, engine state
  positions              open positions
  trades [n]             last n fills (default 15)
  strategy               show active strategy source
  params                 show active strategy PARAMS
  versions               strategy version journal (promoted + rejected attempts)
  evolve                 run a self-improvement cycle now
  rollback <v>           revert to strategy version v
  ask <question>         ask Grok (with live tools + MCP tools)
  mcp                    list MCP tools available to Grok
  pause | resume         stop / restart placing orders (data + evolution keep running)
  flatten                close all positions and pause
  reset-halt             clear the daily-loss kill switch (use with care)
  clear                  clear the screen"""


class Engine:
    def __init__(self, cfg: Config, bus: EventBus | None = None):
        self.cfg = cfg
        self.bus = bus or EventBus(DATA_DIR / "events.jsonl")
        if cfg.broker == "alpaca":
            self.feed = AlpacaFeed(cfg.alpaca_key, cfg.alpaca_secret, cfg.symbols, cfg.bar_timeframe)
            self.broker: Broker = AlpacaBroker(cfg.alpaca_key, cfg.alpaca_secret, live=cfg.alpaca_live)
        else:
            self.feed = SyntheticFeed(cfg.symbols)
            self.broker = PaperBroker(self.feed.last_price)
        self.risk = RiskManager(cfg.risk, cfg.symbols)
        self.brain = Brain(cfg, self.bus)
        self.evolver = Evolver(cfg, self.bus, self.brain)
        self.strategy: dict = {}
        self.paused = False
        self.ticks = 0
        self.strategy_errors = 0
        self.equity_curve: list[tuple[float, float]] = []
        self.trades: list[dict] = []
        self._day = None
        self._last_risk_notes: list[str] = []
        self._tasks: set[asyncio.Task] = set()
        self._register_brain_tools()
        self.state_path = DATA_DIR / "state.json"
        self._save_every = max(1, int(30 / max(cfg.tick_seconds, 0.01)))  # ~every 30 s
        if cfg.persist_state:
            self.load_state()

    # ---------- persistence (survives Codespace / container restarts) ----------
    def save_state(self) -> None:
        snap = {"saved": time.time(), "broker": self.cfg.broker, "symbols": self.cfg.symbols,
                "ticks": self.ticks, "paused": self.paused, "day": self._day,
                "day_start_equity": self.risk.state.day_start_equity if self.risk.state else None,
                "halted": bool(self.risk.state and self.risk.state.halted),
                "halt_reason": self.risk.state.halt_reason if self.risk.state else "",
                "equity_curve": self.equity_curve[-2000:], "trades": self.trades[-500:]}
        if isinstance(self.broker, PaperBroker):
            snap["paper"] = self.broker.snapshot()
        if isinstance(self.feed, SyntheticFeed):
            snap["feed"] = self.feed.snapshot()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, default=str))
        os.replace(tmp, self.state_path)

    def load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            snap = json.loads(self.state_path.read_text())
        except (OSError, ValueError) as e:
            self.bus.emit("warn", f"could not read saved state ({e}); starting fresh")
            return
        if snap.get("broker") != self.cfg.broker or snap.get("symbols") != self.cfg.symbols:
            self.bus.emit("warn", "saved state is for a different broker/symbol list; starting fresh")
            return
        if "feed" in snap and isinstance(self.feed, SyntheticFeed) and not self.feed.restore(snap["feed"]):
            return
        if "paper" in snap and isinstance(self.broker, PaperBroker):
            self.broker.restore(snap["paper"])
        self.ticks, self.paused, self._day = snap["ticks"], snap["paused"], snap["day"]
        self.equity_curve = [tuple(x) for x in snap["equity_curve"]]
        self.trades = snap["trades"]
        if snap.get("day_start_equity"):
            self.risk.start_day(snap["day_start_equity"])
            self.risk.state.halted, self.risk.state.halt_reason = snap["halted"], snap["halt_reason"]
        age = (time.time() - snap["saved"]) / 60
        self.bus.emit("info", f"resumed saved state from {age:.0f} min ago (tick {self.ticks}"
                              + (", PAUSED" if self.paused else "") + (", HALTED" if snap["halted"] else "") + ")")

    # ---------- lifecycle ----------
    def load_active_strategy(self) -> None:
        self.strategy = load_strategy(ACTIVE.read_text(), str(ACTIVE))
        self.strategy_errors = 0
        self.bus.emit("info", f"strategy v{self.evolver.current_version} loaded "
                              f"(params: {json.dumps(self.strategy['PARAMS'])})")

    async def run(self) -> None:
        mode = "LIVE MONEY" if self.cfg.is_live_money else self.broker.name
        self.bus.emit("info", f"engine starting | broker={mode} | symbols={','.join(self.cfg.symbols)} | "
                              f"AI={self.cfg.llm_model if self.brain.available else 'offline (mutation-only)'}")
        if self.cfg.is_live_money:
            self.bus.emit("risk", "*** LIVE TRADING WITH REAL MONEY ENABLED ***")
        self.load_active_strategy()
        if self.brain.mcp.configured:
            await self.brain.mcp.connect()
        while True:
            t0 = time.monotonic()
            try:
                await asyncio.to_thread(self.tick)
            except Exception as e:
                self.bus.emit("error", f"tick failed: {type(e).__name__}: {e}")
            if self.cfg.evolve_every_ticks and self.ticks and self.ticks % self.cfg.evolve_every_ticks == 0:
                self.spawn(self.run_evolution())
            if self.cfg.persist_state and self.ticks % self._save_every == 0:
                try:
                    await asyncio.to_thread(self.save_state)
                except Exception as e:
                    self.bus.emit("warn", f"state save failed: {e}")
            await asyncio.sleep(max(0.1, self.cfg.tick_seconds - (time.monotonic() - t0)))

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # ---------- one trading step ----------
    def tick(self) -> None:
        self.feed.advance()
        self.ticks += 1
        equity = self.broker.equity()
        self.equity_curve.append((time.time(), equity))
        self.equity_curve = self.equity_curve[-5000:]
        self._maybe_new_day(equity)

        if self.risk.check_kill_switch(equity):
            if self.broker.positions():
                self.bus.emit("risk", f"KILL SWITCH: {self.risk.state.halt_reason} — flattening")
                for f in self.broker.close_all():
                    self._log_fill(f)
            return
        if self.paused or not self.broker.market_open():
            return

        history = self.feed.history(LOOKBACK)
        positions = self.broker.positions()
        cur_w = {s: p.market_value / equity for s, p in positions.items()} if equity > 0 else {}
        try:
            raw = self.strategy["generate_signals"](history, cur_w, dict(self.strategy["PARAMS"]))
            self.strategy_errors = 0
        except Exception as e:
            self.strategy_errors += 1
            self.bus.emit("error", f"strategy error #{self.strategy_errors}: {type(e).__name__}: {e}")
            if self.strategy_errors >= 3:
                self._auto_rollback()
            return
        weights, notes = self.risk.sanitize_weights(raw)
        kinds = [re.sub(r"[-+]?\d[\d.]*", "#", n) for n in notes]  # same warning, different number = same
        if kinds != self._last_risk_notes:  # log risk adjustments when they change, not every tick
            for n in notes:
                self.bus.emit("risk", n)
            self._last_risk_notes = kinds
        self._rebalance(weights, positions, equity)

    def _rebalance(self, weights: dict[str, float], positions, equity: float) -> None:
        min_notional = max(1.0, REBALANCE_BAND * equity)
        orders = []
        for sym in self.cfg.symbols:
            px = self.feed.last_price(sym)
            cur = positions[sym].qty if sym in positions else 0.0
            target = weights.get(sym, 0.0) * equity / px
            delta = target - cur
            closing = target == 0 and cur != 0
            if abs(delta * px) < min_notional and not closing:
                continue
            qty = self.risk.clamp_order_qty(abs(delta), px)
            qty = round(qty, 4) if self.cfg.broker == "paper_sim" or "/" in sym else round(qty, 3)
            if qty > 0:
                orders.append((sym, qty, "buy" if delta > 0 else "sell", px))
        for sym, qty, side, px in sorted(orders, key=lambda o: o[2] != "sell"):  # sells first
            try:
                self._log_fill(self.broker.submit_order(sym, qty, side), px)
            except Exception as e:
                self.bus.emit("error", f"order {side} {qty} {sym} failed: {e}")

    def _log_fill(self, f: dict, ref_px: float | None = None) -> None:
        f = dict(f)
        f.setdefault("price", ref_px)
        f["tick"] = self.ticks
        self.trades.append(f)
        self.trades = self.trades[-1000:]
        px = f.get("price")
        self.bus.emit("trade", f"{f.get('side', '?').upper():4} {f.get('qty')} {f.get('symbol')}"
                               + (f" @ {px:.2f}" if isinstance(px, (int, float)) else "")
                               + f"  [{f.get('status', '')}]", fill=f)

    def _maybe_new_day(self, equity: float) -> None:
        day = (self.ticks // SIM_BARS_PER_DAY if self.cfg.broker == "paper_sim"
               else datetime.now(timezone.utc).date().isoformat())
        if day != self._day:
            self._day = day
            self.risk.start_day(equity)
            self.bus.emit("info", f"new trading day ({day}); start equity {equity:,.2f}")

    def _auto_rollback(self) -> None:
        prev = [e["version"] for e in self.evolver.journal if e.get("promoted") and e["version"] < self.evolver.current_version]
        if not prev:
            self.paused = True
            self.bus.emit("risk", "strategy keeps crashing and no earlier version exists — pausing")
            return
        self.bus.emit("risk", f"strategy crashed 3x in a row — auto-rollback to v{prev[-1]}")
        self.evolver.rollback(prev[-1])
        self.load_active_strategy()

    # ---------- self-improvement ----------
    def backtest_bars(self) -> dict:
        if isinstance(self.feed, SyntheticFeed):
            return {s: b[-3000:] for s, b in self.feed.bars.items()}
        return {s: b[-3000:] for s, b in self.feed._fetch(lookback_days=45).items()}

    def live_summary(self) -> dict:
        eq = [e for _, e in self.equity_curve]
        positions = self.broker.positions()
        return {
            "equity": round(eq[-1], 2) if eq else None,
            "pnl_since_start": round(eq[-1] - eq[0], 2) if eq else 0,
            "pnl_today": round(eq[-1] - self.risk.state.day_start_equity, 2) if eq and self.risk.state else 0,
            "ticks": self.ticks,
            "strategy_version": self.evolver.current_version,
            "positions": {s: {"qty": round(p.qty, 4), "avg": round(p.avg_price, 2), "upnl": round(p.unrealized_pnl, 2)}
                          for s, p in positions.items()},
            "halted": bool(self.risk.state and self.risk.state.halted),
            "paused": self.paused,
        }

    async def run_evolution(self) -> dict:
        try:
            bars = await asyncio.to_thread(self.backtest_bars)
            res = await self.evolver.evolve(bars, self.live_summary(), self.trades[-50:])
            if res.get("status") == "promoted":
                self.load_active_strategy()
            return res
        except Exception as e:
            self.bus.emit("error", f"evolution failed: {type(e).__name__}: {e}")
            return {"status": "error"}

    async def submit_strategy(self, source: str, rationale: str, author: str) -> dict:
        """External AI (e.g. via MCP) proposes a full strategy file; same sandbox + holdout gate as internal ones."""
        self.bus.emit("ai", f"strategy submitted by {author}: {rationale[:200]}")
        bars = await asyncio.to_thread(self.backtest_bars)
        res = await self.evolver.evolve(bars, self.live_summary(), self.trades[-50:],
                                        submitted=[(author[:40], rationale, source)])
        if res.get("status") == "promoted":
            self.load_active_strategy()
        return res

    async def backtest_source(self, source: str) -> dict:
        """Walk-forward backtest of arbitrary strategy source without promoting it."""
        bars = await asyncio.to_thread(self.backtest_bars)
        return await self.evolver.evaluate(source, bars, f"adhoc{int(time.time() * 1000)}")

    # ---------- tools the built-in AI can call ----------
    def _register_brain_tools(self) -> None:
        async def status(_):
            return self.live_summary()

        async def trades(args):
            return self.trades[-int(args.get("n", 20)):]

        async def strategy(_):
            return {"version": self.evolver.current_version, "source": ACTIVE.read_text()}

        async def journal(_):
            return [{k: e.get(k) for k in ("version", "author", "promoted", "reason", "metrics")} for e in self.evolver.journal[-15:]]

        async def recent_bars(args):
            sym = str(args.get("symbol", self.cfg.symbols[0])).upper()
            return self.feed.history(int(min(200, args.get("n", 50)))).get(sym, [])

        self.brain.register_tool("bot_status", "Live equity, P&L, positions and engine state.", status)
        self.brain.register_tool("bot_trades", "Recent fills.", trades,
                                 {"type": "object", "properties": {"n": {"type": "integer"}}})
        self.brain.register_tool("bot_strategy", "Active strategy source code and version.", strategy)
        self.brain.register_tool("bot_journal", "History of strategy versions, including rejected attempts.", journal)
        self.brain.register_tool("bot_bars", "Recent OHLCV bars for a symbol.", recent_bars,
                                 {"type": "object", "properties": {"symbol": {"type": "string"}, "n": {"type": "integer"}},
                                  "required": ["symbol"]})

    # ---------- terminal commands ----------
    def status_line(self) -> dict:
        s = self.live_summary()
        state = "HALTED" if s["halted"] else "PAUSED" if self.paused else "RUNNING"
        return {"type": "status", "state": state, "broker": "LIVE" if self.cfg.is_live_money else self.broker.name,
                "equity": s["equity"], "pnl_today": s["pnl_today"], "pnl_total": s["pnl_since_start"],
                "version": s["strategy_version"], "ticks": self.ticks, "positions": len(s["positions"]),
                "evolving": self.evolver.lock.locked(), "ai": self.cfg.llm_model if self.brain.available else "offline",
                "spark": [round(e, 2) for _, e in self.equity_curve[-120:]]}

    async def command(self, line: str) -> str:
        out = await self._command(line)
        if self.cfg.persist_state and line.strip().split(" ")[0].lower() in {"pause", "resume", "flatten", "reset-halt", "rollback"}:
            await asyncio.to_thread(self.save_state)  # operator decisions must survive a restart
        return out

    async def _command(self, line: str) -> str:
        parts = line.strip().split(maxsplit=1)
        if not parts:
            return ""
        cmd, arg = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        if cmd == "help":
            return HELP
        if cmd == "status":
            return json.dumps(self.live_summary(), indent=2, default=str)
        if cmd == "positions":
            ps = self.broker.positions()
            if not ps:
                return "no open positions"
            rows = [f"{'SYMBOL':8}{'QTY':>12}{'AVG':>12}{'LAST':>12}{'VALUE':>14}{'UPNL':>12}"]
            rows += [f"{s:8}{p.qty:>12.4f}{p.avg_price:>12.2f}{p.market_price:>12.2f}{p.market_value:>14,.2f}{p.unrealized_pnl:>+12,.2f}"
                     for s, p in ps.items()]
            return "\n".join(rows)
        if cmd == "trades":
            n = int(arg) if arg.isdigit() else 15
            return "\n".join(f"#{t.get('tick')} {t.get('side','').upper():4} {t.get('qty')} {t.get('symbol')} @ {t.get('price')}"
                             for t in self.trades[-n:]) or "no trades yet"
        if cmd == "strategy":
            return f"# v{self.evolver.current_version}  ({ACTIVE})\n" + ACTIVE.read_text()
        if cmd == "params":
            return json.dumps(self.strategy.get("PARAMS", {}), indent=2)
        if cmd == "versions":
            rows = []
            for e in self.evolver.journal[-25:]:
                h = (e.get("metrics") or {}).get("holdout") or {}
                mark = "*" if e.get("is_active") else ("+" if e.get("promoted") else "x")
                rows.append(f"{mark} v{e['version']:<4} {e['author'][:12]:12} score={h.get('score', '—')!s:>7} "
                            f"sharpe={h.get('sharpe', '—')!s:>6}  {e.get('reason') or e.get('rationale', '')[:70]}")
            return "* active   + promoted   x rejected\n" + "\n".join(rows)
        if cmd == "evolve":
            if self.evolver.lock.locked():
                return "evolution already running"
            self.spawn(self.run_evolution())
            return "evolution cycle started — watch the log"
        if cmd == "rollback":
            try:
                self.evolver.rollback(int(arg.lstrip("v")))
                self.load_active_strategy()
                return f"now running v{self.evolver.current_version}"
            except (ValueError, StrategyRejected) as e:
                return f"rollback failed: {e}"
        if cmd == "ask":
            if not self.brain.available:
                return "AI is offline: set LLM_API_KEY in .env (any OpenAI-compatible provider)"
            if not arg:
                return "usage: ask <question>"
            self.bus.emit("ai", f"> {arg}")
            try:
                return await self.brain.chat(ASK_SYSTEM, arg)
            except Exception as e:
                return f"Grok error: {e}"
        if cmd == "mcp":
            if not self.brain.mcp.configured:
                return "no MCP server configured (MCP_SERVER_URL or MCP_SERVER_COMMAND)"
            await self.brain.mcp.connect()
            return "\n".join(f"{t['function']['name'][5:]}: {t['function']['description'][:90]}"
                             for t in self.brain.mcp.tools) or "MCP connected but exposes no tools"
        if cmd == "pause":
            self.paused = True
            self.bus.emit("warn", "trading paused by operator")
            return "paused"
        if cmd == "resume":
            self.paused = False
            self.bus.emit("info", "trading resumed by operator")
            return "resumed"
        if cmd == "flatten":
            self.paused = True
            for f in await asyncio.to_thread(self.broker.close_all):
                self._log_fill(f)
            self.bus.emit("warn", "operator flattened all positions; trading paused")
            return "flattened and paused"
        if cmd == "reset-halt":
            if self.risk.state:
                self.risk.start_day(self.broker.equity())
            self.bus.emit("risk", "kill switch reset by operator (day start equity re-based)")
            return "halt cleared"
        return f"unknown command: {cmd} (type 'help')"


__all__ = ["Engine", "VERSIONS"]

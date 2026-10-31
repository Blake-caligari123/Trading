"""Trading engine: data -> strategy -> risk -> orders, plus scheduling of self-improvement."""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from .backtest import LOOKBACK, REBALANCE_BAND
from .brain import Brain
from .analysis import indicators
from .brokers import AlpacaBroker, Broker, PaperBroker
from .config import DATA_DIR, Config
from .data import AlpacaFeed, SyntheticFeed
from .desk import Desk
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
  ask <question>         ask the built-in AI (with live tools + MCP tools)
  mcp                    list MCP tools available to the built-in AI
  overview               all symbols: price, trend, RSI, position, strategy signal
  analyze <sym> [tf]     indicators for one symbol (tf: 5Min 15Min 30Min 1Hour 4Hour 1Day)
  buy <sym> <size> [sl=] [tp=]   size: 10 (shares) | $5000 | 5%  (of equity)
  sell <sym> <size> [sl=] [tp=]  sells a long / opens a short if ALLOW_SHORT
  close <sym> [pct]      close a position (default 100%)
  release <sym>          hand a desk-traded symbol back to the strategy
  mode auto|manual       auto: strategy trades; manual: only desk orders
  journal [n]            desk orders with reasons and realized P&L
  pause | resume         stop / restart opening trades (stops + exits keep working)
  flatten                close all positions and pause
  reset-halt             clear the daily-loss kill switch (use with care)
  clear                  clear the screen"""


def fmt_desk(r: dict) -> str:
    """One-line, phone-friendly summary of a desk order result."""
    st = r.get("status")
    if st == "rejected":
        hint = f"  (max allowed: {r['max_allowed_qty']} ≈ {r.get('max_allowed_notional', 0):,.2f})" if r.get("max_allowed_qty") is not None else ""
        return f"REJECTED: {r['reason']}{hint}"
    if st in ("filled", "preview"):
        line = (f"{'FILLED' if st == 'filled' else 'PREVIEW'} {r['side'].upper()} {r['qty']} {r['symbol']} @ "
                f"{r.get('fill_price', r['est_price'])}  ≈ {r['est_notional']:,.2f}\n"
                f"position {r['position_before']} → {r['position_after']} ({r['weight_after']:+.1%} of equity), "
                f"gross {r['gross_exposure_after']:.0%}  SL {r['stop_loss'] or '—'}  TP {r['take_profit'] or '—'}")
        if r.get("realized_pnl"):
            line += f"\nrealized P&L {r['realized_pnl']:+,.2f}"
        return line
    return r.get("reason") or json.dumps(r)


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
        self.paused_by: str | None = None
        self.lock = threading.RLock()   # serializes ticks and desk orders
        self.desk = Desk(self)
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
                "equity_curve": self.equity_curve[-2000:], "trades": self.trades[-500:],
                "paused_by": self.paused_by, "desk": self.desk.snapshot()}
        if isinstance(self.broker, PaperBroker):
            snap["paper"] = self.broker.snapshot()
        if isinstance(self.feed, SyntheticFeed):
            snap["feed"] = self.feed.snapshot()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, default=str))
        os.replace(tmp, self.state_path)

    def persist(self) -> None:
        if self.cfg.persist_state:
            try:
                self.save_state()
            except Exception as e:
                self.bus.emit("warn", f"state save failed: {e}")

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
        self.paused_by = snap.get("paused_by")
        if "desk" in snap:
            self.desk.restore(snap["desk"])
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
        with self.lock:
            self._tick()

    def _tick(self) -> None:
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
        if not self.broker.market_open():
            return
        self.desk.check_exits()            # stop-loss / take-profit run even while paused
        if self.paused or self.desk.mode == "manual":
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
        locked = self.desk.locked()
        if locked:  # desk-controlled symbols keep their size; strategy gets whatever gross exposure is left
            locked_gross = sum(abs(cur_w.get(s, 0.0)) for s in locked)
            free = {s: w for s, w in weights.items() if s not in locked}
            room = max(0.0, self.cfg.risk.max_gross_exposure - locked_gross)
            g = sum(abs(w) for w in free.values())
            weights = {s: w * room / g for s, w in free.items()} if g > room > 0 or (g and room == 0) else free
        self._rebalance(weights, positions, equity, skip=locked)

    def _rebalance(self, weights: dict[str, float], positions, equity: float, skip: set[str] = frozenset()) -> None:
        min_notional = max(1.0, REBALANCE_BAND * equity)
        orders = []
        for sym in self.cfg.symbols:
            if sym in skip:
                continue
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

    def _log_fill(self, f: dict, ref_px: float | None = None, quiet: bool = False) -> None:
        f = dict(f)
        f.setdefault("price", ref_px)
        f["tick"] = self.ticks
        self.trades.append(f)
        self.trades = self.trades[-1000:]
        px = f.get("price")
        if quiet:
            return
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
                "mode": self.desk.mode, "desk": sorted(self.desk.manual),
                "equity": s["equity"], "pnl_today": s["pnl_today"], "pnl_total": s["pnl_since_start"],
                "version": s["strategy_version"], "ticks": self.ticks, "positions": len(s["positions"]),
                "evolving": self.evolver.lock.locked(), "ai": self.cfg.llm_model if self.brain.available else "offline",
                "spark": [round(e, 2) for _, e in self.equity_curve[-120:]]}

    def _cmd_order(self, side: str, arg: str) -> str:
        a = arg.split()
        if len(a) < 2:
            return f"usage: {side} <sym> <qty | $notional | pct%> [sl=price] [tp=price]"
        sym, size = a[0], a[1]
        kw: dict = {}
        try:
            if size.startswith("$"):
                kw["notional"] = float(size[1:].replace(",", ""))
            elif size.endswith("%"):
                kw["percent_equity"] = float(size[:-1])
            else:
                kw["qty"] = float(size)
            for t in a[2:]:
                k, _, v = t.partition("=")
                if k in ("sl", "stop"):
                    kw["stop_loss"] = float(v)
                elif k in ("tp", "target"):
                    kw["take_profit"] = float(v)
        except ValueError:
            return "could not parse size / sl / tp"
        return fmt_desk(self.desk.order(sym, side, reason="operator terminal order", author="operator", **kw))

    # ---------- pause / resume (an AI can't override an operator's pause) ----------
    def pause(self, by: str) -> str:
        self.paused = True
        if self.paused_by != "operator":
            self.paused_by = by
        self.bus.emit("warn", f"trading paused by {by}")
        return f"paused (by {self.paused_by})"

    def resume(self, by: str) -> str:
        if self.paused and self.paused_by == "operator" and by != "operator":
            return "refused: trading was paused by the operator; only the operator can resume it"
        self.paused, self.paused_by = False, None
        self.bus.emit("info", f"trading resumed by {by}")
        return "resumed"

    # ---------- research helpers (used by MCP tools and terminal) ----------
    def clock(self) -> dict:
        c = getattr(self.broker, "clock", None) or getattr(self.feed, "clock", None)
        try:
            return c() if c else {"is_open": self.broker.market_open()}
        except Exception as e:
            return {"error": str(e)}

    def strategy_signals(self) -> dict:
        """What the active strategy wants right now (target weights after risk clamps)."""
        with self.lock:
            equity = self.broker.equity()
            positions = self.broker.positions()
            cur_w = {s: p.market_value / equity for s, p in positions.items()} if equity > 0 else {}
            try:
                raw = self.strategy["generate_signals"](self.feed.history(LOOKBACK), cur_w, dict(self.strategy["PARAMS"]))
            except Exception as e:
                return {"error": f"strategy error: {e}"}
        w, notes = self.risk.sanitize_weights(raw)
        return {"strategy_version": self.evolver.current_version,
                "target_weights": {s: round(w.get(s, 0.0), 4) for s in self.cfg.symbols},
                "current_weights": {s: round(cur_w.get(s, 0.0), 4) for s in self.cfg.symbols},
                "risk_notes": notes, "desk_symbols_ignored_by_strategy": sorted(self.desk.locked())}

    def analyze(self, symbol: str, timeframe: str = "5Min", n: int = 200) -> dict:
        sym = symbol.upper()
        if sym not in self.cfg.symbols:
            return {"error": f"{sym} not in universe {self.cfg.symbols}"}
        bars = self.feed.get_bars(sym, timeframe, n)
        return {"symbol": sym, "timeframe": timeframe, **indicators(bars)}

    def market_overview(self) -> dict:
        sig = self.strategy_signals()
        equity = self.broker.equity()
        positions = self.broker.positions()
        locked = self.desk.locked()
        rows = []
        for s in self.cfg.symbols:
            ind = indicators(self.feed.get_bars(s, "5Min", 200))
            q = self.feed.quote(s)
            rows.append({"symbol": s, "last": q.get("last") or ind.get("last"), "day_change": q.get("day_change"),
                         "trend": ind.get("trend"), "rsi_14": ind.get("rsi_14"), "atr_pct": ind.get("atr_pct"),
                         "position_weight": round(positions[s].market_value / equity, 4) if s in positions and equity else 0.0,
                         "strategy_target_weight": (sig.get("target_weights") or {}).get(s, 0.0),
                         "managed_by": "desk" if s in locked else "strategy"})
        return {"timeframe": "5Min", "clock": self.clock(), "mode": self.desk.mode, "symbols": rows}

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
            return self.pause("operator")
        if cmd == "resume":
            return self.resume("operator")
        if cmd in ("buy", "sell"):
            return self._cmd_order(cmd, arg)
        if cmd == "close":
            a = arg.split()
            if not a:
                return "usage: close <sym> [pct]"
            pct = float(a[1].rstrip("%")) if len(a) > 1 else 100.0
            return fmt_desk(await asyncio.to_thread(self.desk.close, a[0], pct, "operator close", "operator"))
        if cmd == "release":
            return json.dumps(self.desk.release(arg.strip()), indent=2) if arg.strip() else "usage: release <sym>"
        if cmd == "mode":
            return json.dumps(self.desk.set_mode(arg.strip() or "?"), indent=2)
        if cmd == "journal":
            n = int(arg) if arg.isdigit() else 15
            rows = [f"#{j['id']} {time.strftime('%m-%d %H:%M', time.localtime(j['ts']))} {j['author'][:14]:14} "
                    f"{j['side'].upper():4} {j['qty']} {j['symbol']} @ {j['price']}  pnl {j['realized_pnl']:+.2f}  {j['reason'][:60]}"
                    for j in self.desk.journal[-n:]]
            return "\n".join(rows) or "no desk orders yet"
        if cmd == "overview":
            ov = await asyncio.to_thread(self.market_overview)
            rows = [f"{'SYM':7}{'LAST':>11}{'DAY':>8}{'TREND':>16}{'RSI':>6}{'POS%':>7}{'SIG%':>7}  BY"]
            for r in ov["symbols"]:
                rows.append(f"{r['symbol']:7}{r['last']:>11.2f}{(r['day_change'] or 0):>+8.2%}{r['trend']:>16}"
                            f"{(r['rsi_14'] or 0):>6.0f}{r['position_weight']:>+7.1%}{r['strategy_target_weight']:>+7.1%}  {r['managed_by']}")
            return "\n".join(rows)
        if cmd == "analyze":
            a = arg.split()
            if not a:
                return "usage: analyze <sym> [timeframe]"
            return json.dumps(await asyncio.to_thread(self.analyze, a[0], a[1] if len(a) > 1 else "5Min"), indent=2)
        if cmd == "flatten":
            self.paused, self.paused_by = True, "operator"
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

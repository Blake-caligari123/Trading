"""Trading desk: direct (discretionary) orders from an AI or the operator, alongside the automatic strategy.

Rules
- Every order goes through the same hard limits as the strategy: symbol allowlist, max position size,
  max gross exposure, max order size, no shorting unless ALLOW_SHORT, an orders-per-hour cap, kill switch.
- Orders that REDUCE risk (selling a long, covering a short, closing) are always allowed, even when the
  kill switch is active or trading is paused.
- A symbol traded manually is taken over from the strategy ("locked") until release() is called,
  so the strategy doesn't immediately undo the trade. mode="manual" hands every symbol to the desk.
- Optional stop-loss / take-profit levels are watched by the engine every tick.
- Rejections explain why and include the largest size that would have been accepted.
"""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import Engine

EPS = 1e-9


class Desk:
    def __init__(self, engine: "Engine"):
        self.e = engine
        self.mode = "auto"                      # auto: strategy trades unlocked symbols | manual: desk only
        self.manual: dict[str, dict] = {}       # symbol -> {stop, target, since, reason, author}
        self.journal: list[dict] = []           # every desk order with its reason and outcome
        self.order_times: list[float] = []

    # ---------- persistence ----------
    def snapshot(self) -> dict:
        return {"mode": self.mode, "manual": self.manual, "journal": self.journal[-1000:],
                "order_times": self.order_times[-200:]}

    def restore(self, snap: dict) -> None:
        self.mode = snap.get("mode", "auto")
        self.manual = snap.get("manual", {})
        self.journal = snap.get("journal", [])
        self.order_times = snap.get("order_times", [])

    # ---------- helpers ----------
    def locked(self) -> set[str]:
        return set(self.e.cfg.symbols) if self.mode == "manual" else set(self.manual)

    def _orders_last_hour(self) -> int:
        cutoff = time.time() - 3600
        self.order_times = [t for t in self.order_times if t > cutoff]
        return len(self.order_times)

    def _round(self, sym: str, qty: float, whole: bool) -> float:
        """Round DOWN so a size computed from a notional/percent never exceeds that notional or a limit."""
        if whole:  # e.g. Alpaca doesn't allow fractional short sales
            return float(math.floor(qty + EPS))
        d = 4 if self.e.cfg.broker == "paper_sim" else (6 if "/" in sym else 3)
        return math.floor(qty * 10 ** d + EPS) / 10 ** d

    def _reject(self, reason: str, **extra) -> dict:
        self.e.bus.emit("warn", f"desk order rejected: {reason}")
        return {"status": "rejected", "reason": reason, **extra}

    # ---------- orders ----------
    def order(self, symbol: str, side: str, qty: float | None = None, notional: float | None = None,
              percent_equity: float | None = None, stop_loss: float | None = None, take_profit: float | None = None,
              reason: str = "", author: str = "operator", dry_run: bool = False) -> dict:
        with self.e.lock:
            return self._order(symbol, side, qty, notional, percent_equity, stop_loss, take_profit, reason, author, dry_run)

    def _order(self, symbol, side, qty, notional, percent_equity, stop_loss, take_profit, reason, author, dry_run):
        e, lim = self.e, self.e.cfg.risk
        sym, side = str(symbol).upper().strip(), str(side).lower().strip()
        if sym not in e.cfg.symbols:
            return self._reject(f"{sym} is not in the tradable universe {e.cfg.symbols}")
        if side not in ("buy", "sell"):
            return self._reject("side must be 'buy' or 'sell'")
        sizes = [x for x in (qty, notional, percent_equity) if x is not None]
        if len(sizes) != 1 or sizes[0] <= 0:
            return self._reject("give exactly one positive size: qty, notional (account currency) or percent_equity")

        px = e.feed.last_price(sym)
        equity = e.broker.equity()
        if equity <= 0 or px <= 0:
            return self._reject("no valid equity/price")
        q = qty if qty is not None else (notional / px if notional is not None else percent_equity / 100 * equity / px)
        positions = e.broker.positions()
        cur = positions[sym].qty if sym in positions else 0.0
        signed = q if side == "buy" else -q
        new = cur + signed
        reducing = abs(new) < abs(cur) - EPS and (new * cur >= 0)   # shrinks the position without flipping
        whole = e.cfg.broker == "alpaca" and "/" not in sym and (new < 0 or cur < 0)
        q = self._round(sym, q, whole)
        if q <= 0:
            return self._reject("size rounds to zero")
        signed = q if side == "buy" else -q
        new = cur + signed

        gross_now = sum(abs(p.market_value) for p in positions.values()) / equity
        gross_after = gross_now - abs(cur * px) / equity + abs(new * px) / equity
        w_after = new * px / equity
        state = e.risk.state

        if not reducing:
            if state and state.halted:
                return self._reject(f"kill switch active ({state.halt_reason}); only risk-reducing orders allowed")
            if e.paused:
                return self._reject(f"trading is paused (by {e.paused_by or 'operator'}); only risk-reducing orders allowed")
            if new < -EPS and not lim.allow_short:
                return self._reject("short selling is disabled (ALLOW_SHORT=false)",
                                    max_allowed_qty=round(max(cur, 0.0), 6))
            # Largest order in this direction that passes EVERY limit at once (so the hint is always usable).
            same_dir = cur * signed >= 0
            pos_room = max(0.0, lim.max_position_pct * equity / px - abs(cur)) if same_dir \
                else abs(cur) + lim.max_position_pct * equity / px
            gross_room = max(0.0, (lim.max_gross_exposure - gross_now) * equity / px) + (0.0 if same_dir else 2 * abs(cur))
            max_q = max(0.0, min(pos_room, gross_room, lim.max_order_notional / px) * 0.999)
            hint = {"max_allowed_qty": self._round(sym, max_q, whole), "max_allowed_notional": round(max_q * px, 2)}
            if abs(w_after) > lim.max_position_pct + EPS:
                return self._reject(f"position would be {abs(w_after):.1%} of equity > max {lim.max_position_pct:.0%}", **hint)
            if gross_after > lim.max_gross_exposure + EPS and gross_after > gross_now:
                return self._reject(f"gross exposure would be {gross_after:.1%} > max {lim.max_gross_exposure:.0%}", **hint)
            if q * px > lim.max_order_notional + EPS:
                return self._reject(f"order notional {q * px:,.2f} > max per order {lim.max_order_notional:,.2f}", **hint)
            if not dry_run and self._orders_last_hour() >= lim.max_manual_orders_per_hour:
                return self._reject(f"rate limit: {lim.max_manual_orders_per_hour} desk orders per hour reached")

        if new > EPS:
            if stop_loss is not None and stop_loss >= px:
                return self._reject(f"stop_loss {stop_loss} must be below the price {px:.4f} for a long position")
            if take_profit is not None and take_profit <= px:
                return self._reject(f"take_profit {take_profit} must be above the price {px:.4f} for a long position")
        elif new < -EPS:
            if stop_loss is not None and stop_loss <= px:
                return self._reject(f"stop_loss {stop_loss} must be above the price {px:.4f} for a short position")
            if take_profit is not None and take_profit >= px:
                return self._reject(f"take_profit {take_profit} must be below the price {px:.4f} for a short position")

        market_open = e.broker.market_open()
        preview = {"symbol": sym, "side": side, "qty": q, "est_price": round(px, 4), "est_notional": round(q * px, 2),
                   "position_before": round(cur, 6), "position_after": round(new, 6),
                   "weight_after": round(w_after, 4), "gross_exposure_after": round(gross_after, 4),
                   "risk_reducing": reducing, "stop_loss": stop_loss, "take_profit": take_profit,
                   "market_open": market_open}
        if dry_run:
            return {"status": "preview", **preview, "note": "all checks passed; nothing was sent"}
        if not market_open:
            return self._reject("market is closed", **{"clock": self.e.clock()})

        avg_before = positions[sym].avg_price if sym in positions else px
        fill = e.broker.submit_order(sym, q, side)
        fill_px = fill.get("price") or px
        fill = dict(fill, source=author)
        e._log_fill(fill, px, quiet=True)   # the DESK line below is the log entry
        self.order_times.append(time.time())

        closed_qty = min(abs(cur), q) if (cur * signed) < 0 else 0.0
        realized = (fill_px - avg_before) * closed_qty * (1 if cur > 0 else -1) if closed_qty else 0.0
        entry = {"id": len(self.journal) + 1, "ts": time.time(), "author": author, "symbol": sym, "side": side,
                 "qty": q, "price": round(fill_px, 4), "reason": reason[:1000], "position_after": round(new, 6),
                 "stop_loss": stop_loss, "take_profit": take_profit, "realized_pnl": round(realized, 2),
                 "equity_before": round(equity, 2)}
        self.journal.append(entry)

        if abs(new) <= EPS:  # flat: exits no longer apply, but the symbol stays with the desk until released
            self.manual[sym] = {"stop": None, "target": None, "since": time.time(), "reason": reason, "author": author}
        else:
            m = self.manual.get(sym, {})
            flipped = cur * new < 0
            self.manual[sym] = {
                "stop": stop_loss if stop_loss is not None else (None if flipped else m.get("stop")),
                "target": take_profit if take_profit is not None else (None if flipped else m.get("target")),
                "since": m.get("since", time.time()), "reason": reason or m.get("reason", ""), "author": author}
        e.bus.emit("trade", f"DESK {author}: {side.upper()} {q} {sym} @ {fill_px:.4f}"
                            + (f"  SL {stop_loss}" if stop_loss else "") + (f"  TP {take_profit}" if take_profit else "")
                            + (f"  realized {realized:+.2f}" if closed_qty else "") + (f" — {reason[:120]}" if reason else ""))
        e.persist()
        return {"status": "filled", **preview, "fill_price": round(fill_px, 4), "realized_pnl": round(realized, 2),
                "journal_id": entry["id"], "managed_by": "desk (strategy won't touch this symbol until released)"}

    def close(self, symbol: str, percent: float = 100.0, reason: str = "", author: str = "operator") -> dict:
        sym = str(symbol).upper()
        pos = self.e.broker.positions().get(sym)
        if not pos or abs(pos.qty) <= EPS:
            return {"status": "noop", "reason": f"no open position in {sym}"}
        pct = max(0.0, min(100.0, float(percent)))
        q = abs(pos.qty) * pct / 100
        if pct >= 100:
            q = abs(pos.qty)
        return self.order(sym, "sell" if pos.qty > 0 else "buy", qty=q, reason=reason or "close position", author=author)

    def set_exits(self, symbol: str, stop_loss: float | None = None, take_profit: float | None = None,
                  author: str = "operator") -> dict:
        sym = str(symbol).upper()
        pos = self.e.broker.positions().get(sym)
        if not pos:
            return {"status": "rejected", "reason": f"no open position in {sym}"}
        px = self.e.feed.last_price(sym)
        long = pos.qty > 0
        if stop_loss is not None and (stop_loss >= px if long else stop_loss <= px):
            return {"status": "rejected", "reason": f"stop_loss must be {'below' if long else 'above'} price {px:.4f}"}
        if take_profit is not None and (take_profit <= px if long else take_profit >= px):
            return {"status": "rejected", "reason": f"take_profit must be {'above' if long else 'below'} price {px:.4f}"}
        with self.e.lock:
            m = self.manual.setdefault(sym, {"since": time.time(), "reason": "", "author": author})
            m["stop"], m["target"] = stop_loss, take_profit
        self.e.bus.emit("info", f"DESK {author}: exits for {sym} set: SL {stop_loss}  TP {take_profit}")
        self.e.persist()
        return {"status": "ok", "symbol": sym, "stop_loss": stop_loss, "take_profit": take_profit, "price": round(px, 4)}

    def release(self, symbol: str) -> dict:
        sym = str(symbol).upper()
        with self.e.lock:
            had = self.manual.pop(sym, None)
        self.e.persist()
        if had is None:
            return {"status": "noop", "reason": f"{sym} was not under desk control"}
        self.e.bus.emit("info", f"DESK: {sym} handed back to the strategy")
        note = " (mode is manual, so the strategy still won't trade until mode=auto)" if self.mode == "manual" else ""
        return {"status": "ok", "symbol": sym, "note": "the strategy will now manage this symbol" + note}

    def set_mode(self, mode: str, author: str = "operator") -> dict:
        mode = str(mode).lower()
        if mode not in ("auto", "manual"):
            return {"status": "rejected", "reason": "mode must be 'auto' or 'manual'"}
        self.mode = mode
        self.e.bus.emit("info", f"trading mode -> {mode.upper()} (by {author})"
                                + (": strategy will not trade; only desk orders" if mode == "manual" else ""))
        self.e.persist()
        return {"status": "ok", "mode": mode}

    # ---------- exits (called by the engine every tick, inside the engine lock) ----------
    def check_exits(self) -> None:
        positions = self.e.broker.positions()
        for sym, m in list(self.manual.items()):
            pos = positions.get(sym)
            if not pos or (m.get("stop") is None and m.get("target") is None):
                continue
            px = self.e.feed.last_price(sym)
            long = pos.qty > 0
            hit = None
            if m.get("stop") is not None and (px <= m["stop"] if long else px >= m["stop"]):
                hit = f"stop-loss {m['stop']} hit at {px:.4f}"
            elif m.get("target") is not None and (px >= m["target"] if long else px <= m["target"]):
                hit = f"take-profit {m['target']} hit at {px:.4f}"
            if hit:
                self._order(sym, "sell" if long else "buy", abs(pos.qty), None, None, None, None,
                            hit, "auto-exit", False)

    # ---------- account view ----------
    def account(self) -> dict:
        e, lim = self.e, self.e.cfg.risk
        equity = e.broker.equity()
        positions = e.broker.positions()
        gross = sum(abs(p.market_value) for p in positions.values())
        net = sum(p.market_value for p in positions.values())
        locked = self.locked()
        pos_rows = []
        for s, p in positions.items():
            m = self.manual.get(s, {})
            pos_rows.append({"symbol": s, "qty": round(p.qty, 6), "avg_price": round(p.avg_price, 4),
                             "last": round(p.market_price, 4), "value": round(p.market_value, 2),
                             "weight": round(p.market_value / equity, 4) if equity else None,
                             "unrealized_pnl": round(p.unrealized_pnl, 2),
                             "unrealized_pct": round((p.market_price / p.avg_price - 1) * (1 if p.qty > 0 else -1), 5) if p.avg_price else None,
                             "managed_by": "desk" if s in locked else "strategy",
                             "stop_loss": m.get("stop"), "take_profit": m.get("target")})
        headroom = {}
        for s in e.cfg.symbols:
            cur_val = positions[s].market_value if s in positions else 0.0
            by_pos = lim.max_position_pct * equity - max(cur_val, 0.0)
            by_gross = lim.max_gross_exposure * equity - gross
            headroom[s] = round(max(0.0, min(by_pos, by_gross, lim.max_order_notional)), 2)
        st = e.risk.state
        return {
            "equity": round(equity, 2), "cash": round(e.broker.cash(), 2),
            "pnl_today": round(equity - st.day_start_equity, 2) if st else None,
            "gross_exposure": round(gross / equity, 4) if equity else None,
            "net_exposure": round(net / equity, 4) if equity else None,
            "mode": self.mode, "paused": e.paused, "paused_by": e.paused_by,
            "kill_switch": bool(st and st.halted), "kill_switch_reason": st.halt_reason if st else "",
            "positions": pos_rows,
            "desk_symbols": sorted(locked), "strategy_symbols": sorted(set(e.cfg.symbols) - locked),
            "max_new_long_notional_per_symbol": headroom,
            "limits": {"max_position_pct": lim.max_position_pct, "max_gross_exposure": lim.max_gross_exposure,
                       "max_order_notional": lim.max_order_notional, "max_daily_loss_pct": lim.max_daily_loss_pct,
                       "allow_short": lim.allow_short, "max_desk_orders_per_hour": lim.max_manual_orders_per_hour,
                       "desk_orders_left_this_hour": max(0, lim.max_manual_orders_per_hour - self._orders_last_hour())},
        }

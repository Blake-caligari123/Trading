"""Self-improvement loop.

  incumbent (strategies/active.py)
      │  context: source, walk-forward metrics, live P&L, recent trades, journal of past attempts
      ▼
  candidate  ← Grok rewrites the strategy   (or, with no API key, random parameter mutation)
      │  1. AST/sandbox validation
      │  2. walk-forward backtest in an isolated subprocess (timeout + memory cap)
      │  3. promote only if OUT-OF-SAMPLE score beats incumbent by EVOLVE_MIN_IMPROVEMENT
      ▼        and drawdown stays bounded
  strategies/versions/vNNNN.py  +  journal.json  +  git commit  →  engine hot-reloads

Only files under strategies/ are ever written. Risk limits, broker code and this file are off-limits.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

from .config import ROOT, STRATEGY_DIR, Config
from .events import EventBus
from .sandbox import StrategyRejected, validate_source

ACTIVE = STRATEGY_DIR / "active.py"
VERSIONS = STRATEGY_DIR / "versions"
JOURNAL = VERSIONS / "journal.json"
CANDIDATES = STRATEGY_DIR / "candidates"
BACKTEST_TIMEOUT = 120

SYSTEM_PROMPT = """You are the research brain of an autonomous trading bot. Your job is to improve the bot's
trading strategy by rewriting its Python strategy file. You will be judged ONLY on out-of-sample
(holdout) walk-forward performance after costs, so prefer robust, simple ideas over curve-fitting.

Hard contract for the file you return:
- Define PARAMS (a dict) and generate_signals(history, positions, params) -> {symbol: target_weight}.
- history: {symbol: [ {t,o,h,l,c,v}, ... ]} oldest->newest (<=200 bars). positions: {symbol: weight}.
- Weights are fractions of equity. Long-only unless told otherwise; each position is clamped to the
  max position size and gross exposure is capped by the risk layer you cannot change.
- Only `import math` / `import statistics`. No I/O, no classes, no dunder/underscore attribute access,
  no eval/exec/open/getattr/type. Must be deterministic and fast (called once per bar per backtest).
- Account for ~3 bps transaction cost per trade: avoid needless churn.

You may call tools to inspect the bot or gather market context before answering.
Respond with:
RATIONALE: <2-5 sentences on what you changed and why, referencing the evidence>
```python
<the complete new strategy file>
```"""


class Evolver:
    def __init__(self, cfg: Config, bus: EventBus, brain=None):
        self.cfg, self.bus, self.brain = cfg, bus, brain
        self.lock = asyncio.Lock()
        VERSIONS.mkdir(parents=True, exist_ok=True)
        CANDIDATES.mkdir(parents=True, exist_ok=True)
        self.journal: list[dict] = json.loads(JOURNAL.read_text()) if JOURNAL.exists() else []
        if not any(e.get("promoted") for e in self.journal) and ACTIVE.exists():
            self._record(ACTIVE.read_text(), author="seed", rationale="initial strategy", metrics=None, promoted=True)

    # ---------- versions ----------
    @property
    def current_version(self) -> int:
        return next((e["version"] for e in reversed(self.journal) if e.get("is_active")), 0)

    def _next_version(self) -> int:
        return max([e["version"] for e in self.journal], default=0) + 1

    def _record(self, src: str, author: str, rationale: str, metrics: dict | None, promoted: bool,
                reason: str = "") -> dict:
        v = self._next_version()
        (VERSIONS / f"v{v:04d}.py").write_text(src)
        if promoted:
            for e in self.journal:
                e["is_active"] = False
        entry = {"version": v, "ts": time.time(), "author": author, "rationale": rationale[:2000],
                 "metrics": metrics, "promoted": promoted, "is_active": promoted, "reason": reason}
        self.journal.append(entry)
        JOURNAL.write_text(json.dumps(self.journal, indent=1))
        return entry

    def _activate(self, src: str, version: int, message: str) -> None:
        tmp = ACTIVE.with_suffix(".tmp")
        tmp.write_text(src)
        os.replace(tmp, ACTIVE)
        for e in self.journal:
            e["is_active"] = e["version"] == version
        JOURNAL.write_text(json.dumps(self.journal, indent=1))
        if self.cfg.auto_git_commit:
            self._git_commit(message)

    def _git_commit(self, message: str) -> None:
        try:
            subprocess.run(["git", "add", "--", "strategies/active.py", "strategies/versions"], cwd=ROOT,
                           check=True, capture_output=True, timeout=30)
            r = subprocess.run(["git", "commit", "-m", message, "--", "strategies/active.py", "strategies/versions"],
                               cwd=ROOT, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                self.bus.emit("evolve", f"git: committed '{message.splitlines()[0]}'")
                if self.cfg.auto_git_push:
                    p = subprocess.run(["git", "push", "origin", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=60)
                    self.bus.emit("evolve" if p.returncode == 0 else "warn",
                                  "git: pushed to GitHub" if p.returncode == 0 else f"git push failed: {p.stderr.strip()[:200]}")
        except Exception as e:
            self.bus.emit("warn", f"git commit skipped: {e}")

    def rollback(self, version: int) -> str:
        path = VERSIONS / f"v{version:04d}.py"
        if not path.exists():
            raise ValueError(f"no version v{version}")
        src = path.read_text()
        validate_source(src)
        self._activate(src, version, f"strategy: rollback to v{version}")
        self.bus.emit("evolve", f"rolled back to strategy v{version}")
        return src

    # ---------- evaluation ----------
    async def evaluate(self, src: str, bars: dict, tag: str) -> dict:
        """Walk-forward backtest in a separate, resource-limited Python process."""
        validate_source(src)
        sp = CANDIDATES / f"{tag}.py"
        bp = CANDIDATES / f"{tag}.bars.json"
        sp.write_text(src)
        bp.write_text(json.dumps(bars))
        code = f"import sys; sys.path.insert(0, {str(ROOT)!r}); from bot.backtest import cli; cli()"
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-c", code, str(sp), str(bp),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, preexec_fn=_limit_resources)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), BACKTEST_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                return {"error": f"backtest timed out after {BACKTEST_TIMEOUT}s"}
            if proc.returncode != 0:
                return {"error": err.decode()[-800:]}
            return json.loads(out.decode().strip().splitlines()[-1])
        finally:
            for p in (sp, bp):
                p.unlink(missing_ok=True)

    # ---------- the loop ----------
    async def evolve(self, bars: dict, live_summary: dict, recent_trades: list[dict],
                     submitted: list[tuple[str, str, str]] | None = None) -> dict:
        """Run one improvement cycle. `submitted` = [(author, rationale, source)] from an external AI
        (e.g. via the MCP server); those replace the internally generated candidates but face the same gate."""
        if self.lock.locked():
            return {"status": "busy"}
        async with self.lock:
            incumbent_src = ACTIVE.read_text()
            self.bus.emit("evolve", f"evolution cycle started (incumbent v{self.current_version})")
            inc = await self.evaluate(incumbent_src, bars, "incumbent")
            if "error" in inc:
                self.bus.emit("error", f"incumbent backtest failed: {inc['error']}")
                return {"status": "error"}
            inc_score = inc["holdout"].get("score", -99)
            self.bus.emit("evolve", f"incumbent  holdout score={inc_score:+.3f} sharpe={inc['holdout'].get('sharpe')} "
                                    f"mdd={inc['holdout'].get('max_drawdown')}")

            candidates = submitted or await self._propose(incumbent_src, inc, live_summary, recent_trades)
            best = None
            for i, (author, rationale, src) in enumerate(candidates):
                try:
                    m = await self.evaluate(src, bars, f"cand{i}")
                except StrategyRejected as e:
                    self.bus.emit("warn", f"candidate {i} ({author}) rejected by sandbox: {e}")
                    self._record(src, author, rationale, None, False, reason=f"sandbox: {e}")
                    continue
                if "error" in m or "error" in m.get("holdout", {}) or "error" in m.get("in_sample", {}):
                    err = m.get("error") or m.get("holdout", {}).get("error") or m.get("in_sample", {}).get("error")
                    self.bus.emit("warn", f"candidate {i} ({author}) failed backtest: {str(err)[:200]}")
                    self._record(src, author, rationale, m, False, reason=f"backtest error: {str(err)[:300]}")
                    continue
                h = m["holdout"]
                self.bus.emit("evolve", f"candidate {i} ({author}) holdout score={h['score']:+.3f} sharpe={h['sharpe']} "
                                        f"mdd={h['max_drawdown']} trades={h['trades']}")
                if best is None or h["score"] > best[3]["holdout"]["score"]:
                    best = (author, rationale, src, m)
                elif author != "mutation":
                    self._record(src, author, rationale, m, False, reason="beaten by another candidate")

            if best is None:
                self.bus.emit("evolve", "no viable candidate; keeping incumbent")
                return {"status": "kept"}
            author, rationale, src, m = best
            h = m["holdout"]
            mdd_cap = max(inc["holdout"].get("max_drawdown", 0.1) * 1.25, 0.10)
            improved = h["score"] >= inc_score + self.cfg.evolve_min_improvement
            if improved and h["max_drawdown"] <= mdd_cap:
                entry = self._record(src, author, rationale, m, True)
                self._activate(src, entry["version"],
                               f"strategy: v{entry['version']} by {author} (holdout {inc_score:+.3f} -> {h['score']:+.3f})\n\n{rationale[:1500]}")
                self.bus.emit("evolve", f"PROMOTED v{entry['version']} ({author}): holdout score "
                                        f"{inc_score:+.3f} → {h['score']:+.3f}", rationale=rationale)
                return {"status": "promoted", "version": entry["version"], "metrics": m}
            reason = "insufficient improvement" if not improved else f"drawdown {h['max_drawdown']} > cap {mdd_cap:.3f}"
            self._record(src, author, rationale, m, False, reason=reason)
            self.bus.emit("evolve", f"best candidate rejected ({reason}); keeping v{self.current_version}")
            return {"status": "kept", "reason": reason}

    async def _propose(self, src: str, inc: dict, live: dict, trades: list[dict]) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        if self.brain is not None and self.brain.available:
            try:
                out.append(await self._grok_candidate(src, inc, live, trades))
            except Exception as e:
                self.bus.emit("error", f"AI proposal failed: {type(e).__name__}: {e}")
        # always add cheap parameter mutations as a baseline / fallback
        for _ in range(3 if out else 6):
            try:
                out.append(("mutation", *mutate_params(src)))
            except Exception as e:
                self.bus.emit("warn", f"mutation failed: {e}")
                break
        return out

    async def _grok_candidate(self, src: str, inc: dict, live: dict, trades: list[dict]) -> tuple[str, str, str]:
        history = [
            {k: e.get(k) for k in ("version", "author", "promoted", "reason", "rationale")}
            | {"holdout": (e.get("metrics") or {}).get("holdout")}
            for e in self.journal[-12:]
        ]
        prompt = (
            f"Symbols: {self.cfg.symbols}. Allow short: {self.cfg.risk.allow_short}. "
            f"Max position: {self.cfg.risk.max_position_pct}, max gross: {self.cfg.risk.max_gross_exposure}.\n\n"
            f"## Current strategy (v{self.current_version})\n```python\n{src}\n```\n\n"
            f"## Walk-forward backtest of current strategy\n{json.dumps(inc, indent=1)}\n\n"
            f"## Live performance\n{json.dumps(live, indent=1, default=str)}\n\n"
            f"## Last trades\n{json.dumps(trades[-20:], default=str)}\n\n"
            f"## Journal of previous attempts (learn from the rejected ones)\n{json.dumps(history, indent=1, default=str)}\n\n"
            "Propose ONE improved strategy file."
        )
        self.bus.emit("ai", f"asking {self.cfg.llm_model} for a strategy rewrite…")
        reply = await self.brain.chat(SYSTEM_PROMPT, prompt, temperature=0.7)
        code = extract_code(reply)
        m = re.search(r"RATIONALE:\s*(.+?)(?:```|$)", reply, re.S)
        rationale = (m.group(1) if m else reply[:600]).strip()
        self.bus.emit("ai", f"AI: {rationale[:400]}")
        return ("ai", rationale, code)


def extract_code(reply: str) -> str:
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", reply, re.S)
    if not blocks:
        raise StrategyRejected("no ```python``` block in model reply")
    return max(blocks, key=len).strip() + "\n"


def mutate_params(src: str, rng: random.Random | None = None, scale: float = 0.25) -> tuple[str, str]:
    """Perturb numeric PARAMS values in the source. Returns (rationale, new_source)."""
    rng = rng or random.Random()
    tree = ast.parse(src)
    node = next((n for n in tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "PARAMS" for t in n.targets)), None)
    if node is None:
        raise ValueError("no PARAMS dict")
    params = ast.literal_eval(node.value)
    numeric = [k for k, v in params.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not numeric:
        raise ValueError("no numeric params")
    changes = []
    for k in rng.sample(numeric, k=max(1, len(numeric) // 2)):
        old = params[k]
        f = 1 + rng.uniform(-scale, scale)
        new = max(1, round(old * f)) if isinstance(old, int) else round(old * f, 6)
        if new == old and isinstance(old, int):
            new = old + rng.choice([-1, 1]) if old > 1 else old + 1
        params[k] = new
        changes.append(f"{k}: {old} -> {new}")
    body = "PARAMS = {\n" + "".join(f"    {k!r}: {v!r},\n" for k, v in params.items()) + "}"
    lines = src.splitlines(keepends=True)
    new_src = "".join(lines[: node.lineno - 1]) + body + "\n" + "".join(lines[node.end_lineno:])
    return "parameter mutation: " + ", ".join(changes), new_src


def _limit_resources() -> None:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
        resource.setrlimit(resource.RLIMIT_CPU, (BACKTEST_TIMEOUT, BACKTEST_TIMEOUT + 5))
    except Exception:
        pass

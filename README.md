# Self-improving AI trading bot

An automated trading bot that trades through a broker, uses **xAI Grok** (plus any tools from an
**MCP server**) as its brain, **rewrites its own strategy code** and promotes a rewrite only when it
beats the current version out of sample. You monitor and control it from a **terminal-style web page**.

```
 ┌──────────── terminal UI (browser) ────────────┐
 │  live log · status bar · commands · ask Grok  │
 └──────────────────────┬────────────────────────┘
                        │ websocket
 ┌──────────────────────▼────────────────────────┐
 │ engine: bars → strategy → RISK LAYER → orders │──► broker (Alpaca paper/live, or offline sim)
 │           ▲                                   │
 │           │ hot-reload on promotion           │
 │ evolver: Grok (+MCP tools) rewrites strategy  │
 │   → sandbox check → isolated walk-forward     │
 │   → promote only if holdout score improves    │
 │   → versions/ + journal + git commit          │
 └───────────────────────────────────────────────┘
```

## Quick start (offline, no keys)

```bash
pip install -r requirements.txt
cp .env.example .env
python -m bot            # open http://127.0.0.1:8000/
```

The default `BROKER=paper_sim` runs a synthetic market with simulated fills, so you can watch the
whole loop (trading, evolving, promoting, rolling back) right away. With no `XAI_API_KEY` set, the bot
still improves itself by mutating its parameters; with Grok enabled it rewrites the logic itself.
To make the demo fast: `TICK_SECONDS=0.5 EVOLVE_EVERY_TICKS=60 python -m bot`.

> Synthetic markets have long-lasting trends, so backtest Sharpe ratios there look absurdly high.
> Expect much more modest numbers on real data.

## Going further

| Step | `.env` |
|---|---|
| Turn on the AI brain | `XAI_API_KEY=...`, `GROK_MODEL=grok-4` (or any model your xAI account has) |
| Connect your MCP server ("grokbot") | `MCP_SERVER_URL=https://…/mcp` (+ `MCP_SERVER_TOKEN`) **or** `MCP_SERVER_COMMAND="npx -y your-mcp-server"` |
| Real market data + Alpaca paper account | `BROKER=alpaca`, `ALPACA_API_KEY`, `ALPACA_API_SECRET`, `TICK_SECONDS=300` |
| Real money | additionally `ALPACA_LIVE=true` **and** `LIVE_TRADING_CONFIRM=I_ACCEPT_REAL_MONEY_RISK` |

Every MCP tool is offered to Grok (as `mcp__<name>`) whenever it chats or writes a strategy, alongside
the bot's own read-only tools (`bot_status`, `bot_trades`, `bot_strategy`, `bot_journal`, `bot_bars`).
Crypto works with Alpaca too: use symbols like `BTC/USD`.

## Terminal commands

`help` · `status` · `positions` · `trades [n]` · `strategy` · `params` · `versions` · `evolve` ·
`rollback <v>` · `ask <question>` · `mcp` · `pause` · `resume` · `flatten` · `reset-halt` · `clear`

Use ↑/↓ for history and Tab to complete.

## How self-improvement stays safe

The bot edits **only `strategies/active.py`**. Everything that protects your money is outside its reach:

- **Hard risk layer** (`bot/risk.py`, configured in `.env`): per-symbol cap, gross-exposure cap,
  per-order notional cap, symbol allowlist, no shorting unless enabled, and a **daily-loss kill
  switch** that flattens everything and halts.
- **Sandbox** (`bot/sandbox.py`): AI-written code may import only `math`/`statistics`; no I/O,
  `eval`/`exec`, `getattr` or dunder/underscore access; and it runs with stripped builtins.
- **Isolated evaluation**: candidates are backtested in a separate process with CPU, memory and
  wall-clock limits.
- **Out-of-sample gate**: the last 30% of the data is held out. A candidate is promoted only if its
  holdout score beats the incumbent's by `EVOLVE_MIN_IMPROVEMENT` and its drawdown stays bounded.
- **Memory and rollback**: every attempt, including rejected ones and the reason, goes into
  `strategies/versions/journal.json` and is shown to Grok next time. Promotions are git-committed.
  `rollback <v>` reverts instantly, and three strategy crashes in a row trigger an automatic rollback.

This reduces risk; it doesn't remove it. Backtests overfit, and live fills, gaps and outages differ
from simulations. Run on Alpaca paper for a long while before considering real money, and only
trade money you can afford to lose.

## Layout

```
bot/config.py       env config + live-trading guard
bot/engine.py       trading loop, terminal commands, tools exposed to Grok
bot/risk.py         hard limits + kill switch (not AI-editable)
bot/brain.py        Grok client with tool-calling + MCP bridge
bot/evolve.py       self-improvement loop, versions, journal, git
bot/sandbox.py      validation / restricted loading of strategy code
bot/backtest.py     walk-forward backtester (same interface as live)
bot/brokers/        paper simulator, Alpaca
bot/data.py         synthetic feed, Alpaca market data
bot/server.py       FastAPI + websocket
bot/web/index.html  the terminal
strategies/active.py   ← the code the bot rewrites
tests/              pytest -q
```

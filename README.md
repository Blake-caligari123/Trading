# Self-improving AI trading bot

An automated trading bot that trades through a broker, **rewrites its own strategy code**, and promotes a
rewrite only when it beats the current version out of sample. You monitor and control it from a
**terminal-style web page** that works on desktop and phone, and is published on GitHub Pages.

It connects to AI in both directions over **MCP**:
- **Any AI tool → bot.** The bot *is* an MCP server (`/mcp`). Claude, ChatGPT, Cursor, Grok, Gemini CLI
  or any other MCP client can watch it, pause it, backtest ideas and submit new strategies.
- **Bot → any AI.** The built-in brain works with any OpenAI-compatible API (Grok by default; also
  OpenAI, OpenRouter, Ollama…) and can use the tools of any MCP server you point it at.

```
 ┌──────────── terminal UI (browser) ────────────┐
 │  live log · status bar · commands · ask AI    │   desktop, phone (installable), GitHub Pages
 └──────────────────────┬────────────────────────┘
                        │ websocket        Claude / ChatGPT / Cursor / Grok …
                        │                         │ MCP  (/mcp, Bearer token)
 ┌──────────────────────▼────────────────────────┐
 │ engine: bars → strategy → RISK LAYER → orders │──► broker (Alpaca paper/live, or offline sim)
 │           ▲                                   │
 │           │ hot-reload on promotion           │
 │ evolver: AI (+MCP tools) rewrites strategy    │◄── submit_strategy from any MCP client
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
whole loop (trading, evolving, promoting, rolling back) right away. With no `LLM_API_KEY` set, the bot
still improves itself by mutating its parameters; with an AI connected it rewrites the logic itself.
To make the demo fast: `TICK_SECONDS=0.5 EVOLVE_EVERY_TICKS=60 python -m bot`.

> Synthetic markets have long-lasting trends, so backtest Sharpe ratios there look absurdly high.
> Expect much more modest numbers on real data.

## Going further

| Step | `.env` |
|---|---|
| Turn on the built-in AI brain | `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`: any OpenAI-compatible API (examples in `.env.example`) |
| Give that AI an MCP server's tools ("grokbot" etc.) | `MCP_SERVER_URL=https://…/mcp` (+ `MCP_SERVER_TOKEN`) **or** `MCP_SERVER_COMMAND="npx -y your-mcp-server"` |
| Real market data + Alpaca paper account | `BROKER=alpaca`, `ALPACA_API_KEY`, `ALPACA_API_SECRET`, `TICK_SECONDS=300` |
| Real money | additionally `ALPACA_LIVE=true` **and** `LIVE_TRADING_CONFIRM=I_ACCEPT_REAL_MONEY_RISK` |

Every MCP tool is offered to the built-in AI (as `mcp__<name>`) whenever it chats or writes a strategy,
alongside the bot's own read-only tools (`bot_status`, `bot_trades`, `bot_strategy`, `bot_journal`, `bot_bars`).
Crypto works with Alpaca too: use symbols like `BTC/USD`.

## Connect any AI tool to the bot (MCP server)

The bot serves MCP over Streamable HTTP at **`<bot-url>/mcp`**. Set `DASHBOARD_TOKEN` and send it as
`Authorization: Bearer <token>`. For clients that only accept a URL, use `<bot-url>/mcp?token=<token>`.

| Client | How |
|---|---|
| Claude Code | `claude mcp add --transport http trading-bot https://<bot>/mcp --header "Authorization: Bearer <token>"` |
| Cursor / Windsurf / VS Code | in your MCP config: `{"trading-bot": {"url": "https://<bot>/mcp", "headers": {"Authorization": "Bearer <token>"}}}` |
| Claude.ai, ChatGPT and other remote-connector UIs | add a custom connector with URL `https://<bot>/mcp?token=<token>` |
| stdio-only clients (older Claude Desktop, etc.) | command `npx -y mcp-remote https://<bot>/mcp --header "Authorization: Bearer <token>"` |

Tools: `get_status`, `get_positions`, `get_trades`, `get_log`, `get_market_bars`, `get_strategy`,
`get_versions`, `get_risk_limits`, `backtest_strategy`, `submit_strategy`, `run_evolution`,
`rollback_strategy`, `pause_trading`, `resume_trading`, `flatten_positions`.

On purpose, there is **no** tool to place an arbitrary order or change risk limits. A strategy submitted by
an outside AI goes through the same sandbox and out-of-sample gate as the bot's own, and is logged in the
journal as `mcp:<author>`. Try asking your AI: *"Look at the trading bot's versions and current strategy,
backtest an improvement, and submit it if it's better."*

## Use it on your phone

1. **Make the bot reachable.** Pick one:
   - *Same Wi-Fi:* `HOST=0.0.0.0` and a `DASHBOARD_TOKEN` in `.env`, then open
     `http://<computer-ip>:8000/?token=<token>` on the phone.
   - *From anywhere, no server:* run `cloudflared tunnel --url http://localhost:8000` next to the bot. It
     prints an `https://….trycloudflare.com` address. (Set `DASHBOARD_TOKEN` first; the tunnel is public.)
   - *24/7:* deploy the `Dockerfile` to a VPS, Fly.io, Railway or Render (with `DASHBOARD_TOKEN` set).
2. **Open the terminal**, either from the bot's own URL or from GitHub Pages (below), then use
   **Share → Add to Home Screen** (iOS) or **Install app** (Android) to get a full-screen app icon.

On a phone, the command chips above the prompt cover everything common. `ask…` pre-fills a question.
The app reconnects by itself when you come back to it.

## On GitHub

- **Terminal on GitHub Pages:** `.github/workflows/pages.yml` publishes `bot/web/` to
  **https://blake-caligari123.github.io/Trading/** on every change. The first time you open it, enter
  your bot's https URL and token (saved only on that device). You can also open it pre-filled:
  `…/Trading/?server=https://<bot>&token=<token>`. The address is stripped from the bar after loading.
  Pages is https, so the bot URL must be https too (a tunnel or hosted deploy, not plain `http://`).
- **Codespaces (the bot runs on GitHub):** see the next section.
- **CI:** `.github/workflows/tests.yml` runs the test suite on every push.

## Terminal commands

`help` · `status` · `positions` · `trades [n]` · `strategy` · `params` · `versions` · `evolve` ·
`rollback <v>` · `ask <question>` · `mcp` · `pause` · `resume` · `flatten` · `reset-halt` · `clear`

Use ↑/↓ for history and Tab to complete. On a phone, tap the chips.

## Run it on GitHub Codespaces (no computer needed)

**First time (about 2 minutes):**
1. Optional: in GitHub **Settings → Codespaces → Secrets**, add `LLM_API_KEY` (and `LLM_BASE_URL`,
   `LLM_MODEL`, or the Alpaca keys) for this repo. Skip it to run offline.
2. Optional: in **Settings → Codespaces → Default idle timeout**, pick **240 minutes** (the maximum).
3. On the repo page: **Code → Codespaces → Create codespace on the default branch**.

When it finishes loading, the bot **starts by itself**, port 8000 is made **public**, and a terminal shows a
box with everything you need: the terminal link, the Pages link, and the URLs and keys for Claude and Grok.
Reprint it at any time with `bash scripts/codespace.sh connect`.

**After it stops** (idle timeout, or GitHub's 12-hour maximum): go to **github.com/codespaces** (works from
your phone's browser) and open the same Codespace again. The bot restarts by itself and **continues where it
left off**: same simulated account, positions, P&L history and strategy versions. The **address and key stay
the same**, so the Claude and Grok connectors keep working without changes.

Good to know:
- While the Codespace is stopped, the bot isn't trading. Positions held at a real broker (Alpaca, MetaTrader)
  stay open with no one managing them. Run `flatten` first if you don't want that.
- Free GitHub accounts include about 60 hours a month on a 2-core Codespace. After that it's about $0.18/hour,
  and GitHub asks before charging if you've set a spending limit.
- Evolved strategies are committed **and pushed** to GitHub automatically (`AUTO_GIT_PUSH=true` in the
  Codespace), so they survive even if the Codespace is deleted.
- Use one Codespace and keep reopening it. A *new* Codespace gets a new address (and new key, unless you set the
  `DASHBOARD_TOKEN` secret), so the connectors would need updating.
- Commands: `bash scripts/codespace.sh [start|stop|connect|log]`.

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
  `strategies/versions/journal.json` and is shown to the AI next time. Promotions are git-committed.
  `rollback <v>` reverts instantly, and three strategy crashes in a row trigger an automatic rollback.

This reduces risk; it doesn't remove it. Backtests overfit, and live fills, gaps and outages differ
from simulations. Run on Alpaca paper for a long while before considering real money, and only
trade money you can afford to lose.

## Layout

```
bot/config.py       env config + live-trading guard
bot/engine.py       trading loop, terminal commands, tools exposed to the built-in AI
bot/risk.py         hard limits + kill switch (not AI-editable)
bot/brain.py        OpenAI-compatible AI client with tool-calling + MCP client bridge
bot/mcp_server.py   the bot's own MCP server (/mcp) for external AI tools
bot/evolve.py       self-improvement loop, versions, journal, git
bot/sandbox.py      validation / restricted loading of strategy code
bot/backtest.py     walk-forward backtester (same interface as live)
bot/brokers/        paper simulator, Alpaca
bot/data.py         synthetic feed, Alpaca market data
bot/server.py       FastAPI + websocket + /mcp mount + auth
bot/web/            the terminal (index.html), PWA manifest, service worker, icon
.github/workflows/  GitHub Pages deploy + CI tests
Dockerfile, .devcontainer/   24/7 hosting, Codespaces
strategies/active.py   ← the code the bot rewrites
tests/              pytest -q
```

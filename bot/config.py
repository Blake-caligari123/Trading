"""Configuration loaded from environment / .env. Read once at startup."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STRATEGY_DIR = ROOT / "strategies"
DATA_DIR = ROOT / "data"
LIVE_CONFIRM_PHRASE = "I_ACCEPT_REAL_MONEY_RISK"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _f(name: str, default: float) -> float:
    v = os.getenv(name, "").strip()
    return float(v) if v else default


def _i(name: str, default: int) -> int:
    v = os.getenv(name, "").strip()
    return int(v) if v else default


@dataclass
class RiskLimits:
    max_position_pct: float = 0.20
    max_gross_exposure: float = 1.0
    max_order_notional: float = 5000.0
    max_daily_loss_pct: float = 0.03
    allow_short: bool = False


@dataclass
class Config:
    broker: str = "paper_sim"
    alpaca_key: str = ""
    alpaca_secret: str = ""
    alpaca_live: bool = False
    symbols: list[str] = field(default_factory=lambda: ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"])
    bar_timeframe: str = "5Min"
    tick_seconds: float = 60.0
    llm_api_key: str = ""
    llm_model: str = "grok-4"
    llm_base_url: str = "https://api.x.ai/v1"
    mcp_server_url: str = ""
    mcp_server_token: str = ""
    mcp_server_command: str = ""
    evolve_every_ticks: int = 120
    evolve_min_improvement: float = 0.05
    auto_git_commit: bool = True
    risk: RiskLimits = field(default_factory=RiskLimits)
    host: str = "127.0.0.1"
    port: int = 8000
    dashboard_token: str = ""
    mcp_server_enabled: bool = True
    persist_state: bool = False      # from_env() turns this on; off for tests / embedded use
    auto_git_push: bool = False

    @property
    def is_live_money(self) -> bool:
        return self.broker == "alpaca" and self.alpaca_live

    @classmethod
    def from_env(cls) -> "Config":
        _load_dotenv(ROOT / ".env")
        cfg = cls(
            broker=os.getenv("BROKER", "paper_sim").strip(),
            alpaca_key=os.getenv("ALPACA_API_KEY", ""),
            alpaca_secret=os.getenv("ALPACA_API_SECRET", ""),
            alpaca_live=_b("ALPACA_LIVE", False),
            symbols=[s.strip().upper() for s in os.getenv("SYMBOLS", "SPY,QQQ,AAPL,MSFT,NVDA").split(",") if s.strip()],
            bar_timeframe=os.getenv("BAR_TIMEFRAME", "5Min"),
            tick_seconds=_f("TICK_SECONDS", 60.0),
            # Any OpenAI-compatible chat API. LLM_* wins; XAI_*/GROK_MODEL kept for backwards compatibility.
            llm_api_key=os.getenv("LLM_API_KEY") or os.getenv("XAI_API_KEY", ""),
            llm_model=os.getenv("LLM_MODEL") or os.getenv("GROK_MODEL", "grok-4"),
            llm_base_url=(os.getenv("LLM_BASE_URL") or os.getenv("XAI_BASE_URL", "https://api.x.ai/v1")).rstrip("/"),
            mcp_server_url=os.getenv("MCP_SERVER_URL", ""),
            mcp_server_token=os.getenv("MCP_SERVER_TOKEN", ""),
            mcp_server_command=os.getenv("MCP_SERVER_COMMAND", ""),
            evolve_every_ticks=_i("EVOLVE_EVERY_TICKS", 120),
            evolve_min_improvement=_f("EVOLVE_MIN_IMPROVEMENT", 0.05),
            auto_git_commit=_b("AUTO_GIT_COMMIT", True),
            risk=RiskLimits(
                max_position_pct=_f("MAX_POSITION_PCT", 0.20),
                max_gross_exposure=_f("MAX_GROSS_EXPOSURE", 1.0),
                max_order_notional=_f("MAX_ORDER_NOTIONAL", 5000.0),
                max_daily_loss_pct=_f("MAX_DAILY_LOSS_PCT", 0.03),
                allow_short=_b("ALLOW_SHORT", False),
            ),
            host=os.getenv("HOST", "127.0.0.1"),
            port=_i("PORT", 8000),
            dashboard_token=os.getenv("DASHBOARD_TOKEN", ""),
            mcp_server_enabled=_b("MCP_SERVER_ENABLED", True),
            persist_state=_b("PERSIST_STATE", True),
            auto_git_push=_b("AUTO_GIT_PUSH", False),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.broker not in {"paper_sim", "alpaca"}:
            raise ValueError(f"Unknown BROKER={self.broker!r}")
        if self.broker == "alpaca" and not (self.alpaca_key and self.alpaca_secret):
            raise ValueError("BROKER=alpaca requires ALPACA_API_KEY and ALPACA_API_SECRET")
        if self.alpaca_live and os.getenv("LIVE_TRADING_CONFIRM", "") != LIVE_CONFIRM_PHRASE:
            raise ValueError(
                f"ALPACA_LIVE=true trades real money. Set LIVE_TRADING_CONFIRM={LIVE_CONFIRM_PHRASE} to proceed."
            )
        if self.host not in {"127.0.0.1", "localhost", "::1"} and not self.dashboard_token:
            raise ValueError("Bot exposed beyond localhost: set DASHBOARD_TOKEN (protects the terminal and the /mcp endpoint)")
        r = self.risk
        if not (0 < r.max_position_pct <= 1 and 0 < r.max_gross_exposure <= 2 and 0 < r.max_daily_loss_pct < 1):
            raise ValueError("Risk limits out of sane range")

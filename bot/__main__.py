"""Run the bot + terminal dashboard:  python -m bot"""
import uvicorn

from .config import Config
from .server import create_app


def main() -> None:
    cfg = Config.from_env()
    print(f"Terminal: http://{cfg.host}:{cfg.port}/" + (f"?token=<DASHBOARD_TOKEN>" if cfg.dashboard_token else ""))
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()

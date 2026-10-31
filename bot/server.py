"""FastAPI server: serves the terminal UI and streams engine events over a WebSocket."""
from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from .config import Config
from .engine import Engine

WEB = Path(__file__).parent / "web" / "index.html"


def create_app(cfg: Config | None = None, engine: Engine | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()
    engine = engine or Engine(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine.bus.loop = asyncio.get_running_loop()
        task = asyncio.create_task(engine.run())
        yield
        task.cancel()
        await engine.brain.mcp.close()

    app = FastAPI(title="Trading Bot Terminal", lifespan=lifespan)
    app.state.engine = engine

    def authorized(token: str | None) -> bool:
        return not cfg.dashboard_token or (token is not None and hmac.compare_digest(token, cfg.dashboard_token))

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return WEB.read_text()

    @app.get("/healthz")
    async def health():
        return {"ok": True, "ticks": engine.ticks}

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        if not authorized(sock.query_params.get("token")):
            await sock.close(code=4401)
            return
        await sock.accept()
        q = engine.bus.subscribe()
        await sock.send_json({"type": "history", "events": list(engine.bus.history)[-300:]})
        await sock.send_json(engine.status_line())

        async def pump():
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=1.0)
                    await sock.send_json({"type": "event", **ev})
                except asyncio.TimeoutError:
                    await sock.send_json(engine.status_line())

        async def reader():
            while True:
                msg = await sock.receive_json()
                line = str(msg.get("cmd", ""))[:2000]
                out = await engine.command(line)
                await sock.send_json({"type": "reply", "cmd": line, "text": out})

        tasks = [asyncio.create_task(pump()), asyncio.create_task(reader())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        except WebSocketDisconnect:
            pass
        finally:
            for t in tasks:
                t.cancel()
            engine.bus.unsubscribe(q)

    return app

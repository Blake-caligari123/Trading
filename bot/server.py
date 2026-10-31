"""FastAPI server: terminal UI + WebSocket event stream + MCP endpoint (/mcp) for external AI tools."""
from __future__ import annotations

import asyncio
import hmac
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse

from .config import Config
from .engine import Engine

WEB = Path(__file__).parent / "web"
STATIC = {"manifest.webmanifest": "application/manifest+json", "sw.js": "text/javascript", "icon.svg": "image/svg+xml"}


def access_ok(cfg: Config, headers: dict[str, str], query_token: str | None) -> bool:
    """With DASHBOARD_TOKEN set: a valid token is required (Bearer header or ?token=), from any origin.
    Without it (localhost-only mode): refuse browser requests coming from a different site, so a random
    web page you visit can't drive your bot through ws://127.0.0.1 (cross-site WebSocket hijacking)."""
    if cfg.dashboard_token:
        auth = headers.get("authorization", "")
        supplied = auth[7:] if auth.lower().startswith("bearer ") else query_token
        return bool(supplied) and hmac.compare_digest(supplied, cfg.dashboard_token)
    origin = headers.get("origin")
    if not origin or origin == "null":
        return origin is None  # non-browser clients send no Origin
    return urlparse(origin).netloc == headers.get("host", "")


class GuardedASGI:
    """Applies access_ok() in front of a mounted ASGI app (the MCP endpoint)."""

    def __init__(self, app, cfg: Config):
        self.app, self.cfg = app, cfg

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            token = (parse_qs(scope.get("query_string", b"").decode()).get("token") or [None])[0]
            if not access_ok(self.cfg, headers, token):
                if scope["type"] == "http":
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [(b"content-type", b"application/json"),
                                            (b"www-authenticate", b"Bearer")]})
                    await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                    return
                await send({"type": "websocket.close", "code": 4401})
                return
        await self.app(scope, receive, send)


def create_app(cfg: Config | None = None, engine: Engine | None = None, start_engine: bool = True) -> FastAPI:
    cfg = cfg or Config.from_env()
    engine = engine or Engine(cfg)
    mcp = mcp_app = None
    if cfg.mcp_server_enabled:
        try:
            from .mcp_server import build_mcp
            mcp, mcp_app = build_mcp(engine)
        except ImportError as e:
            engine.bus.emit("warn", f"MCP server disabled ({e}); pip install mcp")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine.bus.loop = asyncio.get_running_loop()
        async with AsyncExitStack() as stack:
            if mcp is not None:
                await stack.enter_async_context(mcp.session_manager.run())
                engine.bus.emit("info", "MCP server live at /mcp — any MCP-capable AI can connect"
                                        + (" (Bearer token required)" if cfg.dashboard_token else " (localhost only)"))
            task = asyncio.create_task(engine.run()) if start_engine else None
            yield
            if task:
                task.cancel()
            await engine.brain.mcp.close()

    app = FastAPI(title="Trading Bot Terminal", lifespan=lifespan)
    app.state.engine = engine

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (WEB / "index.html").read_text()

    def _static(path: Path, media: str):
        async def endpoint():
            return FileResponse(path, media_type=media)
        return endpoint

    for name, media in STATIC.items():  # explicit routes so they never shadow /mcp
        app.add_api_route(f"/{name}", _static(WEB / name, media), methods=["GET"], include_in_schema=False)

    @app.get("/healthz")
    async def health():
        return {"ok": True, "ticks": engine.ticks}

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        headers = {k.lower(): v for k, v in sock.headers.items()}
        if not access_ok(cfg, headers, sock.query_params.get("token")):
            # accept-then-close so the browser actually sees code 4401 (closing before accept surfaces as 1006)
            await sock.accept()
            await sock.close(code=4401, reason="unauthorized")
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

    if mcp_app is not None:
        app.mount("/", GuardedASGI(mcp_app, cfg))  # serves /mcp; mounted last so the routes above win
    return app

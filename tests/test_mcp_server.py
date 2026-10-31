"""End-to-end: run the real server, connect with a real MCP client, call tools."""
import asyncio
import json
import socket
import threading
import time

import httpx
import pytest

mcp = pytest.importorskip("mcp")
uvicorn = pytest.importorskip("uvicorn")

from bot import evolve as ev  # noqa: E402
from bot.config import Config  # noqa: E402
from bot.engine import Engine  # noqa: E402
from bot.events import EventBus  # noqa: E402
from bot.server import access_ok, create_app  # noqa: E402

TOKEN = "s3cret-token"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def server(tmp_path, monkeypatch):
    sdir = tmp_path / "strategies"
    (sdir / "versions").mkdir(parents=True)
    (sdir / "active.py").write_text(ev.ACTIVE.read_text())
    for name, val in {"ACTIVE": sdir / "active.py", "VERSIONS": sdir / "versions",
                      "JOURNAL": sdir / "versions" / "journal.json", "CANDIDATES": sdir / "candidates"}.items():
        monkeypatch.setattr(ev, name, val)
    import bot.engine as eng
    import bot.mcp_server as ms
    monkeypatch.setattr(eng, "ACTIVE", sdir / "active.py")
    monkeypatch.setattr(ms, "ACTIVE", sdir / "active.py")
    port = _free_port()
    cfg = Config(auto_git_commit=False, tick_seconds=0.05, evolve_every_ticks=0, dashboard_token=TOKEN, port=port)
    engine = Engine(cfg, EventBus())
    srv = uvicorn.Server(uvicorn.Config(create_app(cfg, engine), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", engine
    srv.should_exit = True
    t.join(5)


async def _session(url):
    import httpx2
    from mcp.client.streamable_http import streamable_http_client
    hc = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60)
    return hc, mcp.Client(streamable_http_client(url + "/mcp", http_client=hc))


def test_mcp_tools_end_to_end(server):
    url, engine = server

    async def go():
        hc, client = await _session(url)
        async with hc, client as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert {"get_status", "submit_strategy", "backtest_strategy", "pause_trading", "get_risk_limits"} <= names
            assert not any("order" in n or "risk" in n and "get" not in n for n in names)  # no order/limit-changing tools

            status = json.loads((await c.call_tool("get_status", {})).content[0].text)
            assert status["type"] == "status"

            bad = (await c.call_tool("backtest_strategy", {"source": "import os\ndef generate_signals(h,p,q): return {}"})).content[0].text
            assert "rejected by sandbox" in bad

            await c.call_tool("pause_trading", {})
            assert engine.paused

            src = ev.ACTIVE.read_text().replace('"fast": 12', '"fast": 9')
            res = json.loads((await c.call_tool("submit_strategy", {"source": src, "rationale": "faster", "author": "test"})).content[0].text)
            assert res["status"] in {"promoted", "kept"}
            assert engine.evolver.journal[-1]["author"] == "mcp:test"

    asyncio.run(go())


def test_mcp_requires_token(server):
    url, _ = server
    r = httpx.post(url + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    r = httpx.get(url + "/healthz")
    assert r.status_code == 200
    assert httpx.get(url + "/manifest.webmanifest").status_code == 200


def test_access_rules():
    open_cfg = Config()  # localhost, no token
    assert access_ok(open_cfg, {"host": "127.0.0.1:8000"}, None)                                  # CLI / MCP client
    assert access_ok(open_cfg, {"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000"}, None)  # own page
    assert not access_ok(open_cfg, {"host": "127.0.0.1:8000", "origin": "https://evil.example"}, None)
    tok_cfg = Config(dashboard_token="abc")
    assert access_ok(tok_cfg, {"origin": "https://me.github.io"}, "abc")
    assert access_ok(tok_cfg, {"authorization": "Bearer abc"}, None)
    assert not access_ok(tok_cfg, {}, "wrong") and not access_ok(tok_cfg, {}, None)

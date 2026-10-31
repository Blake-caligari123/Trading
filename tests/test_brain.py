import asyncio
import json

import httpx

from bot.brain import Brain
from bot.config import Config
from bot.events import EventBus


def test_grok_tool_loop():
    """Grok asks for a local tool, gets the result, then answers."""
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body)
        assert req.headers["authorization"] == "Bearer k"
        if len(calls) == 1:
            assert any(t["function"]["name"] == "bot_status" for t in body["tools"])
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "bot_status", "arguments": "{}"}}]}
        else:
            tool_msg = body["messages"][-1]
            assert tool_msg["role"] == "tool" and json.loads(tool_msg["content"]) == {"equity": 123}
            msg = {"role": "assistant", "content": "equity is 123"}
        return httpx.Response(200, json={"choices": [{"message": msg}]})

    brain = Brain(Config(xai_api_key="k"), EventBus())
    brain.http = httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler),
                                   headers={"Authorization": "Bearer k"})

    async def status(_):
        return {"equity": 123}

    brain.register_tool("bot_status", "status", status)
    assert asyncio.run(brain.chat("sys", "how much?")) == "equity is 123"
    assert len(calls) == 2

"""The AI brain: xAI Grok (OpenAI-compatible chat completions) + tools from an MCP server.

Grok gets two kinds of tools:
  * local read-only tools exposed by the bot (status, positions, recent trades, backtest metrics)
  * every tool published by the configured MCP server ("grokbot" or any other), proxied through
"""
from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Any, Awaitable, Callable

import httpx

from .config import Config
from .events import EventBus

LocalTool = tuple[dict, Callable[[dict], Awaitable[Any]]]


class MCPBridge:
    """Persistent connection to one MCP server. Works with mcp SDK 2.x (mcp.Client) or 1.x (ClientSession)."""

    def __init__(self, cfg: Config, bus: EventBus):
        self.cfg, self.bus = cfg, bus
        self._stack: AsyncExitStack | None = None
        self._client: Any = None
        self.tools: list[dict] = []

    @property
    def configured(self) -> bool:
        return bool(self.cfg.mcp_server_url or self.cfg.mcp_server_command)

    async def connect(self) -> None:
        if not self.configured or self._client:
            return
        import shlex
        try:
            import mcp
        except ImportError:
            self.bus.emit("warn", "MCP configured but `mcp` package not installed (pip install mcp)")
            return
        self._stack = AsyncExitStack()
        headers = {"Authorization": f"Bearer {self.cfg.mcp_server_token}"} if self.cfg.mcp_server_token else {}
        try:
            if hasattr(mcp, "Client"):  # SDK 2.x
                if self.cfg.mcp_server_url:
                    target: Any = self.cfg.mcp_server_url
                    if headers:
                        import httpx2  # type: ignore
                        from mcp.client.streamable_http import streamable_http_client
                        hc = await self._stack.enter_async_context(httpx2.AsyncClient(headers=headers, timeout=60))
                        target = streamable_http_client(self.cfg.mcp_server_url, http_client=hc)
                else:
                    argv = shlex.split(self.cfg.mcp_server_command)
                    target = mcp.StdioServerParameters(command=argv[0], args=argv[1:])
                self._client = await self._stack.enter_async_context(mcp.Client(target))
            else:  # SDK 1.x
                from mcp import ClientSession
                if self.cfg.mcp_server_url:
                    from mcp.client.streamable_http import streamablehttp_client
                    r, w, *_ = await self._stack.enter_async_context(streamablehttp_client(self.cfg.mcp_server_url, headers=headers))
                else:
                    from mcp import StdioServerParameters
                    from mcp.client.stdio import stdio_client
                    argv = shlex.split(self.cfg.mcp_server_command)
                    r, w = await self._stack.enter_async_context(stdio_client(StdioServerParameters(command=argv[0], args=argv[1:])))
                self._client = await self._stack.enter_async_context(ClientSession(r, w))
                await self._client.initialize()
            res = await self._client.list_tools()
            self.tools = [
                {"type": "function", "function": {
                    "name": f"mcp__{t.name}"[:64],
                    "description": (t.description or "")[:1000],
                    "parameters": t.inputSchema or {"type": "object", "properties": {}},
                }}
                for t in res.tools
            ]
            self.bus.emit("ai", f"MCP connected: {len(self.tools)} tools "
                          f"({', '.join(t['function']['name'][5:] for t in self.tools[:8])}{'…' if len(self.tools) > 8 else ''})")
        except Exception as e:
            self.bus.emit("error", f"MCP connection failed: {type(e).__name__}: {e}")
            await self.close()

    async def call(self, name: str, args: dict) -> str:
        if not self._client:
            return "error: MCP not connected"
        res = await self._client.call_tool(name, args)
        parts = []
        for c in getattr(res, "content", []) or []:
            parts.append(getattr(c, "text", None) or json.dumps(getattr(c, "model_dump", lambda: str(c))(), default=str))
        out = "\n".join(parts) or json.dumps(getattr(res, "structuredContent", None), default=str)
        return ("error: " if getattr(res, "isError", False) else "") + out[:20_000]

    async def close(self) -> None:
        if self._stack:
            try:
                await self._stack.aclose()
            except Exception:
                pass
        self._stack, self._client, self.tools = None, None, []


class Brain:
    def __init__(self, cfg: Config, bus: EventBus):
        self.cfg, self.bus = cfg, bus
        self.mcp = MCPBridge(cfg, bus)
        self.local_tools: dict[str, LocalTool] = {}
        self.http = httpx.AsyncClient(base_url=cfg.xai_base_url, timeout=180,
                                      headers={"Authorization": f"Bearer {cfg.xai_api_key}"})

    @property
    def available(self) -> bool:
        return bool(self.cfg.xai_api_key)

    def register_tool(self, name: str, description: str, fn: Callable[[dict], Awaitable[Any]],
                      parameters: dict | None = None) -> None:
        schema = {"type": "function", "function": {"name": name, "description": description,
                                                   "parameters": parameters or {"type": "object", "properties": {}}}}
        self.local_tools[name] = (schema, fn)

    async def chat(self, system: str, user: str, use_tools: bool = True, max_rounds: int = 6,
                   temperature: float = 0.4) -> str:
        if not self.available:
            raise RuntimeError("XAI_API_KEY not set")
        await self.mcp.connect()
        tools = ([t[0] for t in self.local_tools.values()] + self.mcp.tools) if use_tools else []
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for _ in range(max_rounds):
            body: dict = {"model": self.cfg.grok_model, "messages": messages, "temperature": temperature}
            if tools:
                body["tools"] = tools
            r = await self.http.post("/chat/completions", json=body)
            if r.status_code >= 400:
                raise RuntimeError(f"Grok API {r.status_code}: {r.text[:500]}")
            msg = r.json()["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if not calls:
                return msg.get("content") or ""
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                self.bus.emit("ai", f"tool → {name}({json.dumps(args)[:120]})")
                try:
                    if name in self.local_tools:
                        result = json.dumps(await self.local_tools[name][1](args), default=str)
                    elif name.startswith("mcp__"):
                        result = await self.mcp.call(name[5:], args)
                    else:
                        result = f"error: unknown tool {name}"
                except Exception as e:
                    result = f"error: {type(e).__name__}: {e}"
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": result[:20_000]})
        # out of rounds: force a final answer without tools
        messages.append({"role": "user", "content": "Tool budget exhausted. Give your final answer now."})
        r = await self.http.post("/chat/completions", json={"model": self.cfg.grok_model, "messages": messages})
        r.raise_for_status()
        return r.json()["choices"][0]["message"].get("content") or ""

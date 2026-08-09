"""MCP arms: stdio subprocess, HTTP sidecar, and HTTP remote.

One class covers all three; only the transport and endpoint differ. That is deliberate.
If `mcp_sidecar` and `mcp_remote` ran different client code, the "deploy it closer to
your agent" comparison would be measuring the code difference, not the distance.

`session_reuse=False` reproduces the naive client that re-runs initialize plus tools/list
on every single request. Measuring that path separately is the difference between an
honest benchmark and one that quietly flatters or slanders MCP.
"""

from __future__ import annotations

import json
import sys
import time
from contextlib import AsyncExitStack
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from .base import Arm, ToolResult, ToolSpec, split_upstream


class McpArm(Arm):
    """MCP client arm.

    Args:
        name: Arm label recorded in results (mcp_stdio / mcp_sidecar / mcp_remote / ...).
        transport: "stdio" or "http".
        url: Endpoint for the http transport, e.g. http://host:9111/mcp
        command / args / env: Subprocess spec for the stdio transport.
        session_reuse: When False, tear down and re-establish the session per call.
        tool_filter: Allowlist of tool names. Used by the mcp_filtered arm.
    """

    def __init__(
        self,
        name: str,
        transport: str,
        url: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        session_reuse: bool = True,
        tool_filter: list[str] | None = None,
    ) -> None:
        super().__init__(tool_filter)
        self.name = name
        self.transport = transport
        self.url = url
        self.command = command or sys.executable
        self.args = args or ["-m", "bench.mcpserver.server", "--transport", "stdio"]
        self.env = env
        self.session_reuse = session_reuse
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    # -- session lifecycle -------------------------------------------------

    async def _open(self) -> tuple[AsyncExitStack, ClientSession, dict[str, float]]:
        """Establish one MCP session. Returns the stack, session, and timing breakdown."""
        stack = AsyncExitStack()
        timings: dict[str, float] = {}

        t0 = time.perf_counter()
        if self.transport == "stdio":
            params = StdioServerParameters(command=self.command, args=self.args, env=self.env)
            read, write = await stack.enter_async_context(stdio_client(params))
        elif self.transport == "http":
            if not self.url:
                raise ValueError("http transport requires a url")
            # streamablehttp_client yields (read, write, get_session_id)
            read, write, _ = await stack.enter_async_context(streamablehttp_client(self.url))
        else:
            raise ValueError(f"unknown transport: {self.transport}")
        timings["connect_ms"] = (time.perf_counter() - t0) * 1000.0

        session = await stack.enter_async_context(ClientSession(read, write))

        t1 = time.perf_counter()
        await session.initialize()
        timings["initialize_ms"] = (time.perf_counter() - t1) * 1000.0

        return stack, session, timings

    async def _preflight(self) -> None:
        """Fail fast and legibly when an HTTP MCP endpoint is not answering.

        Catching this after the fact does not work: the MCP client stack runs the
        handshake inside an anyio TaskGroup, so a dead endpoint surfaces as a
        CancelledError inside a BaseExceptionGroup. Those derive from BaseException, so
        `except Exception` never sees them, and the operator gets 300 lines of async
        traceback with `ConnectError` buried in the middle. Probing first sidesteps the
        exception-semantics fight entirely.
        """
        if self.transport != "http" or not self.url:
            return
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                # Any response at all proves something is listening and speaking HTTP.
                # A 4xx/405 here is fine; only a transport failure matters.
                await client.get(self.url)
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"arm '{self.name}' cannot reach its MCP endpoint at {self.url} "
                f"({type(exc).__name__}). Is the server running? "
                f"Start it with: python -m bench.mcpserver.server --transport http "
                f"--port {self.url.split(':')[-1].split('/')[0]}"
            ) from exc

    async def connect(self) -> None:
        await self._preflight()
        await self._connect()

    async def _connect(self) -> None:
        if not self.session_reuse:
            # Nothing persistent to establish; each call opens its own session. Measure
            # the cost once anyway so the report can show what the naive path pays.
            stack, session, timings = await self._open()
            t = time.perf_counter()
            await session.list_tools()
            timings["list_tools_ms"] = (time.perf_counter() - t) * 1000.0
            await stack.aclose()
        else:
            self._stack, self._session, timings = await self._open()
            t = time.perf_counter()
            await self._session.list_tools()
            timings["list_tools_ms"] = (time.perf_counter() - t) * 1000.0

        self.session_cost.connect_ms = timings["connect_ms"]
        self.session_cost.initialize_ms = timings["initialize_ms"]
        self.session_cost.list_tools_ms = timings["list_tools_ms"]

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._session = None

    # -- tools -------------------------------------------------------------

    @staticmethod
    def _to_specs(listed: Any) -> list[ToolSpec]:
        specs: list[ToolSpec] = []
        for tool in listed.tools:
            schema = tool.inputSchema or {"type": "object", "properties": {}}
            specs.append(
                ToolSpec(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=schema,
                )
            )
        return specs

    async def list_tools(self) -> list[ToolSpec]:
        if self.session_reuse:
            if self._session is None:
                raise RuntimeError("arm not connected")
            listed = await self._session.list_tools()
        else:
            stack, session, _ = await self._open()
            try:
                listed = await session.list_tools()
            finally:
                await stack.aclose()
        self._tools = self._apply_filter(self._to_specs(listed))
        return self._tools

    # -- calls -------------------------------------------------------------

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        if self.session_reuse:
            if self._session is None:
                raise RuntimeError("arm not connected")
            return await self._call(self._session, name, args)

        stack, session, _ = await self._open()
        try:
            # A naive client also re-lists tools before calling. Include it, because that
            # is what the path being measured actually costs.
            await session.list_tools()
            return await self._call(session, name, args)
        finally:
            await stack.aclose()

    @staticmethod
    async def _call(session: ClientSession, name: str, args: dict[str, Any]) -> ToolResult:
        try:
            result = await session.call_tool(name, args)
        except Exception as exc:  # noqa: BLE001 - surface protocol errors as tool errors
            return ToolResult(content=json.dumps({"error": str(exc)}), is_error=True)

        parts: list[str] = []
        for block in result.content:
            text = getattr(block, "text", None)
            parts.append(text if text is not None else str(block))
        raw = "\n".join(parts)
        content, upstream_ms = split_upstream(raw)
        return ToolResult(
            content=content,
            upstream_ms=upstream_ms,
            is_error=bool(getattr(result, "isError", False)),
        )

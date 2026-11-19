#!/usr/bin/env python3
"""Expose a local SSE MCP server over stdio, for clients that only speak stdio/streamable HTTP (OpenBot).

Survives restarts of the SSE server: when the SSE stream drops, in-flight requests get a
JSON-RPC error at once (instead of hanging until the client's idle timeout), the relay
reconnects, and replays the client's initialize handshake on the new server session.
"""
import os
import sys

import anyio
import mcp.types as t
from mcp.client.sse import sse_client
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

RETRY_DELAYS = (0.5, 1, 2, 3, 5)


def log(msg):
    print(f"mcp-bridge: {msg}", file=sys.stderr, flush=True)


async def main(url):
    init_req = None      # client's initialize request, replayed after reconnect
    init_notif = None    # notifications/initialized
    pending = {}         # request id -> method, for requests awaiting a server reply
    to_remote_send, to_remote_recv = anyio.create_memory_object_stream(1000)

    async with stdio_server() as (local_in, local_out):

        async def read_local():
            nonlocal init_req, init_notif
            async for msg in local_in:
                if isinstance(msg, Exception):
                    log(repr(msg))
                    continue
                m = msg.message
                if isinstance(m, t.JSONRPCRequest):
                    if m.method == "initialize":
                        init_req = m
                    pending[m.id] = m.method
                elif isinstance(m, t.JSONRPCNotification) and m.method == "notifications/initialized":
                    init_notif = m
                await to_remote_send.send(msg)
            # Client closed stdin: nothing left to relay. Exit hard (stdin thread may block shutdown).
            os._exit(0)

        async def fail_pending(reason):
            for rid, method in list(pending.items()):
                pending.pop(rid, None)
                err = t.JSONRPCError(jsonrpc="2.0", id=rid, error=t.ErrorData(
                    code=-32000, message=f"MCP server connection lost during {method} ({reason}); retry the call"))
                await local_out.send(SessionMessage(err))

        async with anyio.create_task_group() as tg:
            tg.start_soon(read_local)
            connected_once = False
            attempt = 0
            reinit = 0
            while True:
                try:
                    async with sse_client(url) as (rin, rout):
                        if connected_once:
                            log("reconnected to SSE server")
                            if init_req is not None:
                                reinit += 1
                                rid = f"relay-reinit-{reinit}"
                                await rout.send(SessionMessage(init_req.model_copy(update={"id": rid})))
                                with anyio.fail_after(15):
                                    async for m in rin:
                                        if isinstance(m, Exception):
                                            raise m
                                        if isinstance(m.message, (t.JSONRPCResponse, t.JSONRPCError)) \
                                                and m.message.id == rid:
                                            break
                                if init_notif is not None:
                                    await rout.send(SessionMessage(init_notif))
                        connected_once = True
                        attempt = 0
                        async with anyio.create_task_group() as inner:

                            async def to_remote():
                                async for m in to_remote_recv:
                                    await rout.send(m)

                            inner.start_soon(to_remote)
                            async for m in rin:
                                if isinstance(m, Exception):
                                    raise m
                                if isinstance(m.message, (t.JSONRPCResponse, t.JSONRPCError)):
                                    pending.pop(m.message.id, None)
                                await local_out.send(m)
                            inner.cancel_scope.cancel()
                    reason = "stream closed"
                except Exception as e:  # noqa: BLE001 - any transport failure means reconnect
                    reason = f"{type(e).__name__}: {e}"[:200]
                log(f"SSE connection lost: {reason}")
                await fail_pending(reason)
                await anyio.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
                attempt += 1


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].startswith("http://127.0.0.1:"):
        sys.exit("usage: sse_stdio.py http://127.0.0.1:<port>/sse")
    anyio.run(main, sys.argv[1])

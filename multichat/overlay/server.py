# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Corrgan
"""OBS overlay: local web server that OBS adds as a browser source.

http://127.0.0.1:<port>/            → transparent chat page
http://127.0.0.1:<port>/ws          → WebSocket that delivers new messages

URL parameters of the page:
  fade=30           fade out messages after 30 s (0 = never)
  max=40            maximum number of visible messages
  size=22           font size in px
  platforms=twitch,kick   show only these platforms
  group=2           show only the second channel group (tab order)
  badges=0          hide the platform badges
"""
from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import WSMsgType, web

from ..chat import alerts
from ..chat.models import ChatMessage, Deletion
from ..core.constants import FAVICONS
from ..core.paths import resource

log = logging.getLogger("multichat.overlay")

MAX_CLIENTS = 16
MAX_BACKLOG = 500  # messages queued for a slow client before it is disconnected





def message_payload(m: ChatMessage, alerts_cfg: dict | None = None, groups: list[int] | None = None) -> dict:
    """groups: numbers (1 = first tab) of the channel groups that show the message."""
    return {
        "event": m.event,
        "headline": alerts.headline(m) if m.event else "",
        "alert": bool(m.event) and alerts.is_alert(m, alerts_cfg or alerts.DEFAULT_ALERTS),
        "type": "msg",
        "platform": m.platform,
        "user": m.user,
        "color": m.color,
        "msg_id": m.msg_id,
        "user_id": m.user_id,
        "source": m.source,
        "groups": groups or [],
        "parts": [{"t": k, "v": v, "a": a} for k, v, a in m.parts],
    }


def deletion_payload(e: Deletion) -> dict:
    # source: a ban in one channel must not remove the user's messages of another channel
    return {"type": "del", "platform": e.platform, "source": e.source, "msg_id": e.msg_id, "user_id": e.user_id,
            "all": e.clear_all}


class OverlayServer:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.clients: dict[web.WebSocketResponse, asyncio.Queue] = {}
        self._page: str | None = None
        self.runner: web.AppRunner | None = None
        self.port: int | None = None
        self._lifecycle_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self.runner is not None

    async def start(self, port: int) -> None:
        # A stop requested while the socket is being created must wait for that
        # start, then close it. Concurrent starts must not create orphan runners.
        async with self._lifecycle_lock:
            if self.runner:
                return
            app = web.Application()
            app.router.add_get("/", self._index)
            app.router.add_get("/ws", self._ws)
            runner = web.AppRunner(app)
            try:
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", port).start()
            except BaseException:
                cancelled = await self._cleanup(runner)
                if cancelled is not None:
                    raise cancelled
                raise
            self.runner, self.port = runner, port

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            cancelled = await self._cleanup(self.runner)
            self.runner, self.port = None, None
            if cancelled is not None:
                raise cancelled

    async def _cleanup(self, runner: web.AppRunner | None) -> asyncio.CancelledError | None:
        # Shutdown cancels every task, so cleanup must stay in the owning task:
        # a shielded child could be cancelled before its cleanup body even runs.
        # Finish releasing resources before reporting cancellation to the caller.
        cancelled = None
        while True:
            try:
                try:
                    # Close every client before runner cleanup waits for handlers.
                    for ws in list(self.clients):
                        while True:
                            try:
                                await ws.close()
                                break
                            except asyncio.CancelledError as error:
                                cancelled = error
                finally:
                    self.clients.clear()
                    if runner is not None:
                        await runner.cleanup()
                return cancelled
            except asyncio.CancelledError as error:
                cancelled = error

    def _allowed(self, request: web.Request) -> bool:
        """Only this machine: the Host header stops DNS rebinding, the Origin header stops
        other websites in the browser from reading the chat over the WebSocket."""
        hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        origin = request.headers.get("Origin")
        if request.host not in hosts or (origin is not None and origin not in {f"http://{h}" for h in hosts}):
            log.warning("Overlay: rejected request (Host %r, Origin %r)", request.host, origin)
            return False
        return True

    async def _index(self, request: web.Request) -> web.Response:
        if not self._allowed(request):
            return web.Response(status=403, text="forbidden")
        if self._page is None:
            self._page = resource("overlay.html").read_text(encoding="utf-8").replace("__FAVICONS__", json.dumps(FAVICONS))
        return web.Response(text=self._page, content_type="text/html",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                     # the page only talks to this server and shows https images
                                     "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; "
                                     "style-src 'unsafe-inline'; img-src https: data:; "
                                     f"connect-src 'self' ws://127.0.0.1:{self.port} ws://localhost:{self.port}"})

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        if not self._allowed(request):
            return web.Response(status=403, text="forbidden")
        if len(self.clients) >= MAX_CLIENTS:
            return web.Response(status=503, text="too many overlay clients")
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4096)  # the page never sends anything big
        await ws.prepare(request)
        queue: asyncio.Queue = asyncio.Queue(MAX_BACKLOG)
        self.clients[ws] = queue
        writer = asyncio.ensure_future(self._writer(ws, queue))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.clients.pop(ws, None)
            writer.cancel()
        return ws

    async def _writer(self, ws: web.WebSocketResponse, queue: asyncio.Queue) -> None:
        """One writer per client keeps the order of the messages."""
        try:
            while True:
                await ws.send_str(await queue.get())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 – client gone
            self.clients.pop(ws, None)

    def publish(self, payload: dict) -> None:
        """Thread-safe: may be called from the Qt thread."""
        if self.runner:
            self.loop.call_soon_threadsafe(self._broadcast, json.dumps(payload))

    def _broadcast(self, data: str) -> None:
        for ws, queue in list(self.clients.items()):
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:  # the browser source stopped reading
                log.warning("Overlay: client too slow – disconnecting it")
                self.clients.pop(ws, None)
                asyncio.ensure_future(ws.close())

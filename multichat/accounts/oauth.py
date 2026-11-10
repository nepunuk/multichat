# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Corrgan
"""Login (OAuth in the browser) plus sending and moderating per platform.

Each platform needs its own developer app (client ID + secret), created by the user.
REDIRECT_URI is entered as its redirect URL; for the login the app briefly starts a
local web server that receives the login code.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import threading
import time
import webbrowser
from typing import Callable
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

from ..core.i18n import _
from ..chat.models import ChatMessage
from .store import TokenStore


log = logging.getLogger("multichat.accounts")

REDIRECT_PORT = 17563
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
LOGIN_TIMEOUT = 300

# The login that is waiting for the browser. Only one can listen on REDIRECT_PORT – a new
# attempt cancels it (e.g. the platform showed an error page and never redirected back,
# which used to block every further login until the timeout or a restart).
_waiting: asyncio.Task | None = None


async def _take_over_login() -> None:
    global _waiting
    old, _waiting = _waiting, asyncio.current_task()
    if old is not None and old is not _waiting and not old.done():
        log.info("Login: cancelling the earlier attempt that is still waiting")
        old.cancel()
        await asyncio.wait([old], timeout=5)  # its cleanup frees the port

SUCCESS_HTML = """<!doctype html><meta charset="utf-8"><title>MultiChat</title>
<body style="font-family:sans-serif;background:#18181b;color:#efeff1;display:grid;
place-items:center;height:100vh;margin:0"><div style="text-align:center">
<h2>{title}</h2><p>{text}</p>
</div></body>"""


def success_html() -> str:
    return SUCCESS_HTML.format(
        title=_("Logged in ✓"), text=_("You can close this window and return to MultiChat.")
    )


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status  # HTTP status of the platform API, if any


def _short_error(text: str) -> str:
    try:
        body = json.loads(text)
    except Exception:  # noqa: BLE001
        return text[:200]
    err = body.get("error")
    if isinstance(err, dict):
        return err.get("message") or json.dumps(err)[:200]
    return body.get("message") or body.get("error_description") or str(err or text)[:200]


Creds = Callable[[str], tuple[str, str]]


class Account:
    platform = ""
    name = ""
    auth_url = ""
    token_url = ""
    scopes = ""
    pkce = True
    auth_extra: dict = {}

    def __init__(self, store: TokenStore, creds: Creds, session: Callable[[], aiohttp.ClientSession]) -> None:
        self.store = store
        self.creds = creds
        self._session = session
        self._cache: dict = {}
        self._refresh_lock: asyncio.Lock | None = None
        # bumped on every logout/login: a token response that arrives afterwards belongs to
        # the old session and must not write tokens back (logged in again by accident)
        self.session_gen = 0
        # Logout runs on Qt's thread; refresh/login run on the asyncio thread.
        # Generation checks and their token mutations must be one transaction.
        self._state_lock = threading.RLock()
        # called with the login URL if no browser could be opened (set by the UI)
        self.show_url: Callable[[str], None] | None = None

    # ---- State --------------------------------------------------------- #
    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session()

    @property
    def tokens(self) -> dict:
        return self.store.get(self.platform)

    @property
    def logged_in(self) -> bool:
        return bool(self.tokens.get("access_token"))

    @property
    def user_name(self) -> str:
        return self.tokens.get("user_name", "")

    @property
    def user_id(self) -> str:
        return str(self.tokens.get("user_id", ""))

    def logout(self) -> bool:
        with self._state_lock:
            self.session_gen += 1
            self._cache.clear()
            return self.store.delete(self.platform)

    def extra_headers(self) -> dict:
        return {}

    def _session_generation(self, generation: int | None = None) -> int:
        """Pin an operation to its session, including work before the first API request."""
        with self._state_lock:
            if generation is not None and generation != self.session_gen:
                raise ApiError(
                    _("{platform} session expired – please log in again").format(platform=self.name), status=401
                )
            return self.session_gen

    # ---- OAuth ---------------------------------------------------------- #
    async def login(self) -> dict:
        cid, secret = self.creds(self.platform)
        if not cid or not secret:
            raise ApiError(_("Client ID/secret missing (Accounts → Set up developer apps)"))
        state = secrets.token_urlsafe(16)
        params = {
            "client_id": cid,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": self.scopes,
            "state": state,
            **self.auth_extra,
        }
        verifier = None
        if self.pkce:
            verifier = secrets.token_urlsafe(64)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            params.update(code_challenge=challenge, code_challenge_method="S256")

        await _take_over_login()
        with self._state_lock:
            self.session_gen += 1
            gen = self.session_gen
        loop = asyncio.get_running_loop()
        code_fut: asyncio.Future = loop.create_future()

        async def callback(request: web.Request) -> web.Response:
            q = request.query
            if not secrets.compare_digest(q.get("state", ""), state):
                return web.Response(text=_("Invalid login state – please try again."), status=400)
            if not code_fut.done():
                if "code" in q:
                    code_fut.set_result(q["code"])
                else:
                    code_fut.set_exception(ApiError(q.get("error_description") or q.get("error") or _("Login cancelled")))
            return web.Response(text=success_html(), content_type="text/html")

        app = web.Application()
        app.router.add_get("/callback", callback)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            try:
                await web.TCPSite(runner, "localhost", REDIRECT_PORT).start()
            except OSError as e:
                raise ApiError(_("Port {port} in use – is a login already running? ({error})").format(port=REDIRECT_PORT, error=e)) from e
            url = f"{self.auth_url}?{urlencode(params)}"
            opened = await asyncio.to_thread(webbrowser.open, url)
            if not opened:
                log.warning("%s login: no browser could be opened – showing the link", self.name)
                if self.show_url:
                    self.show_url(url)
            try:
                code = await asyncio.wait_for(code_fut, LOGIN_TIMEOUT)
            except asyncio.TimeoutError as e:
                raise ApiError(_("Login timed out")) from e
        finally:
            await runner.cleanup()

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": cid,
            "client_secret": secret,
        }
        if verifier:
            data["code_verifier"] = verifier
        tokens = await self._token_request(data)
        with self._state_lock:
            if gen != self.session_gen:
                raise ApiError(_("Login cancelled"))
            self.session_gen += 1  # a refresh started while the browser waited still belongs to the old account
            gen = self.session_gen
            self.store.replace(self.platform, tokens)
            self._cache.clear()
        try:
            who = await self.whoami()
        except Exception:
            with self._state_lock:
                if gen == self.session_gen:
                    self.session_gen += 1
                    self.store.delete(self.platform)
            raise
        with self._state_lock:
            if gen != self.session_gen:
                raise ApiError(_("Login cancelled"))
            self.store.update(self.platform, **who)
        return who

    async def _token_request(self, data: dict) -> dict:
        async with self.session.post(self.token_url, data=data, headers={"Accept": "application/json"}) as r:
            text = await r.text()
            status = r.status
        if status >= 400:
            log.warning("%s token request (%s) → HTTP %s: %s", self.name, data.get("grant_type"), status, text[:300])
            raise ApiError(
                _("{platform} login failed ({status}): {error}").format(platform=self.name, status=status, error=_short_error(text)),
                status=status,
            )
        body = json.loads(text)
        out = {
            "access_token": body["access_token"],
            "expires_at": time.time() + float(body.get("expires_in") or 3600),
        }
        if body.get("refresh_token"):
            out["refresh_token"] = body["refresh_token"]
        return out

    async def _refresh(self, stale_token: str | None = None) -> None:
        """Renews the access token. Several requests may need this at the same time; the
        platforms rotate refresh tokens, so only one refresh may run – the others reuse it."""
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        async with self._refresh_lock:
            with self._state_lock:
                tok = self.tokens
                gen = self.session_gen
                if tok.get("access_token") != stale_token and tok.get("expires_at", 0) > time.time() + 60:
                    return  # another request refreshed it meanwhile
            rt = tok.get("refresh_token")
            expired = ApiError(_("{platform} session expired – please log in again").format(platform=self.name), status=401)
            if not rt:
                raise expired
            cid, secret = self.creds(self.platform)
            try:
                new = await self._token_request(
                    {"grant_type": "refresh_token", "refresh_token": rt, "client_id": cid, "client_secret": secret}
                )
            except ApiError as e:
                with self._state_lock:
                    if gen != self.session_gen or self.tokens.get("refresh_token") != rt:
                        raise expired from e  # a different session owns the store now
                    if e.status in (400, 401):
                        log.warning("%s: refresh token rejected – logging out", self.name)
                        self.logout()
                        raise expired from e
                raise
            with self._state_lock:
                if gen != self.session_gen or self.tokens.get("refresh_token") != rt:
                    log.info("%s: logged out during a token refresh – discarding the new token", self.name)
                    raise expired
                self.store.update(self.platform, **new)

    async def request(self, method: str, url: str, *, _retry: bool = True,
                      _generation: int | None = None, **kw):
        expired = ApiError(_("{platform} session expired – please log in again").format(platform=self.name), status=401)
        with self._state_lock:
            gen = self.session_gen if _generation is None else _generation
            if gen != self.session_gen:
                raise expired
            tok = self.tokens
            if not tok.get("access_token"):
                raise ApiError(_("Not logged in to {platform}").format(platform=self.name))
        if tok.get("expires_at", 0) < time.time() + 60:
            await self._refresh(tok.get("access_token"))
        extra = kw.pop("headers", {})
        with self._state_lock:
            if gen != self.session_gen:
                raise expired
            token = self.tokens.get("access_token")
            if not token:
                raise expired
            headers = {"Authorization": f"Bearer {token}", **self.extra_headers(), **extra}
        async with self.session.request(method, url, headers=headers, **kw) as r:
            text = await r.text()
            status = r.status
        with self._state_lock:
            if gen != self.session_gen:
                raise expired  # never retry an old action with a newly logged-in account
        if status == 401 and _retry:
            await self._refresh(token)
            return await self.request(method, url, _retry=False, _generation=gen, headers=extra, **kw)
        if status >= 400:
            log.warning("%s %s %s → HTTP %s: %s", self.name, method, url.split("?")[0], status, text[:300])
            raise ApiError(f"{self.name} ({status}): {_short_error(text)}", status=status)
        log.debug("%s %s %s → HTTP %s", self.name, method, url.split("?")[0], status)
        return json.loads(text) if text.strip() else None

    # ---- implemented by subclasses -------------------------------------- #
    async def whoami(self) -> dict:
        raise NotImplementedError

    async def send(self, channel: str, text: str) -> None:
        raise NotImplementedError

    async def delete(self, msg: ChatMessage) -> None:
        raise NotImplementedError

    async def timeout(self, msg: ChatMessage, seconds: int) -> None:
        raise NotImplementedError

    async def ban(self, msg: ChatMessage) -> None:
        raise NotImplementedError

    async def unban(self, msg: ChatMessage) -> None:
        raise NotImplementedError

    async def is_moderator(self, channel: str) -> bool | None:
        """True/False if known for this channel, None if the platform can't tell us."""
        return None

    async def user_info(self, msg: ChatMessage) -> dict:
        """Extra facts for the user card, e.g. {"created": iso, "following_since": iso}."""
        return {}

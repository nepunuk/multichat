# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Corrgan
"""YouTube login, sending and moderating (YouTube Data API)."""
from __future__ import annotations

import logging
import time

from ..chat.models import ChatMessage
from ..core.i18n import _
from .oauth import Account, ApiError

log = logging.getLogger("multichat.accounts")
from ..chat.youtube import find_live_video, youtube_target


class YouTubeAccount(Account):
    platform = "youtube"
    name = "YouTube"
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    scopes = "https://www.googleapis.com/auth/youtube.force-ssl"
    auth_extra = {"access_type": "offline", "prompt": "consent"}
    API = "https://www.googleapis.com/youtube/v3"

    async def whoami(self) -> dict:
        items = (await self.request("GET", f"{self.API}/channels", params={"part": "snippet", "mine": "true"})).get(
            "items"
        ) or []
        if not items:
            return {"user_id": "", "user_name": "(no channel)"}
        return {"user_id": items[0]["id"], "user_name": items[0]["snippet"]["title"]}

    async def current_video(self, value: str, *, _generation: int | None = None) -> str:
        """The video ID directly, or for a channel input the running live stream (cached 60 s)."""
        gen = self._session_generation(_generation)
        kind, target = youtube_target(value)
        if kind == "video":
            return target
        with self._state_lock:
            self._session_generation(gen)
            hit = self._cache.get(("live", target))
            if hit and hit[1] > time.time():
                return hit[0]
        found = await find_live_video(target)
        with self._state_lock:
            self._session_generation(gen)
            if not found:
                raise ApiError(_("The YouTube channel is not live right now"))
            self._cache[("live", target)] = (found[0], time.time() + 60)
        return found[0]

    async def live_chat_id(self, video: str, *, _generation: int | None = None) -> str:
        gen = self._session_generation(_generation)
        vid = await self.current_video(video, _generation=gen)
        with self._state_lock:
            self._session_generation(gen)
            if vid in self._cache:
                return self._cache[vid]
        items = (
            await self.request("GET", f"{self.API}/videos", _generation=gen,
                               params={"part": "liveStreamingDetails", "id": vid})
        ).get("items") or []
        with self._state_lock:
            self._session_generation(gen)
            chat_id = items and (items[0].get("liveStreamingDetails") or {}).get("activeLiveChatId")
            if not chat_id:
                raise ApiError(_("No active live chat for this video"))
            self._cache[vid] = chat_id
            return chat_id

    async def send(self, channel: str, text: str) -> None:
        gen = self._session_generation()
        chat_id = await self.live_chat_id(channel, _generation=gen)
        await self.request(
            "POST",
            f"{self.API}/liveChat/messages",
            _generation=gen,
            params={"part": "snippet"},
            json={
                "snippet": {
                    "liveChatId": chat_id,
                    "type": "textMessageEvent",
                    "textMessageDetails": {"messageText": text},
                }
            },
        )

    async def delete(self, msg: ChatMessage) -> None:
        await self.request("DELETE", f"{self.API}/liveChat/messages", params={"id": msg.msg_id})

    async def _ban(self, msg: ChatMessage, seconds: int | None) -> None:
        gen = self._session_generation()
        chat_id = await self.live_chat_id(msg.channel, _generation=gen)
        snippet = {
            "liveChatId": chat_id,
            "type": "temporary" if seconds else "permanent",
            "bannedUserDetails": {"channelId": msg.user_id},
        }
        if seconds:
            snippet["banDurationSeconds"] = seconds
        res = await self.request("POST", f"{self.API}/liveChat/bans", _generation=gen,
                                 params={"part": "snippet"}, json={"snippet": snippet})
        with self._state_lock:
            self._session_generation(gen)
            if res and res.get("id"):
                self._cache[("ban", msg.channel, msg.user_id)] = res["id"]

    async def timeout(self, msg: ChatMessage, seconds: int) -> None:
        await self._ban(msg, seconds)

    async def unban(self, msg: ChatMessage) -> None:
        # YouTube can only lift a ban by its ID, which only the ban request returns
        with self._state_lock:
            gen = self._session_generation()
            ban_id = self._cache.get(("ban", msg.channel, msg.user_id))
        if not ban_id:
            raise ApiError(_("YouTube can only lift bans made in MultiChat during this session"))
        await self.request("DELETE", f"{self.API}/liveChat/bans", _generation=gen, params={"id": ban_id})
        with self._state_lock:
            self._session_generation(gen)
            self._cache.pop(("ban", msg.channel, msg.user_id), None)

    async def is_moderator(self, channel: str) -> bool | None:
        gen = self._session_generation()
        vid = await self.current_video(channel, _generation=gen)
        items = (await self.request("GET", f"{self.API}/videos", _generation=gen,
                                    params={"part": "snippet", "id": vid})).get("items") or []
        with self._state_lock:
            self._session_generation(gen)
            if items and items[0]["snippet"].get("channelId") == self.user_id:
                return True  # your own stream
        return None  # moderators of other channels aren't visible via the API

    async def user_info(self, msg: ChatMessage) -> dict:
        if not msg.user_id:
            return {}
        items = (await self.request("GET", f"{self.API}/channels", params={"part": "snippet", "id": msg.user_id})).get(
            "items") or []
        return {"created": items[0]["snippet"].get("publishedAt")} if items else {}

    async def ban(self, msg: ChatMessage) -> None:
        await self._ban(msg, None)

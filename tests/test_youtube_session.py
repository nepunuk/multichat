# SPDX-License-Identifier: GPL-3.0-or-later
"""A channel lookup must not move a pending YouTube action into another login."""
import asyncio
import json
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import pytest

from multichat.accounts import oauth
from multichat.accounts.store import MemoryBackend, SecretStore
from multichat.accounts.youtube import YouTubeAccount
from multichat.chat import youtube as chat_youtube
from multichat.chat.models import ChatMessage


VIDEO = "dQw4w9WgXcQ"
CHANNEL = "@synthetic_channel"


class Reply:
    status = 200

    def __init__(self, body):
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def text(self):
        return json.dumps(self.body)


class FakeAPI:
    """Only replace the HTTP boundary; Account.request and OAuth remain real."""
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/channels"):
            return Reply({"items": [{"id": "B", "snippet": {"title": "Account B"}}]})
        if url.endswith("/videos"):
            return Reply({"items": [{"liveStreamingDetails": {"activeLiveChatId": "CHAT"},
                                     "snippet": {"channelId": "A"}}]})
        if method == "POST" and url.endswith("/liveChat/bans"):
            return Reply({"id": "BAN"})
        return Reply({})

    def post(self, url, *, data, headers):
        assert url == YouTubeAccount.token_url
        assert data["grant_type"] == "authorization_code"
        return Reply({"access_token": "SYNTHETIC_B", "refresh_token": "RT_B", "expires_in": 3600})


def account_and_api():
    api = FakeAPI()
    store = SecretStore("tokens", MemoryBackend())
    store.replace("youtube", {"access_token": "SYNTHETIC_A", "refresh_token": "RT_A",
                              "expires_at": time.time() + 3600, "user_id": "A"})
    return YouTubeAccount(store, lambda _: ("TEST_ID", "TEST_SECRET"), lambda: api), api


def local_browser(monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    monkeypatch.setattr(oauth, "REDIRECT_PORT", port)
    monkeypatch.setattr(oauth, "REDIRECT_URI", f"http://localhost:{port}/callback")

    def browser(url):
        state = parse_qs(urlparse(url).query)["state"][0]
        with urlopen(f"{oauth.REDIRECT_URI}?code=TEST_CODE&state={state}", timeout=3) as response:
            assert response.status == 200
        return True

    monkeypatch.setattr(oauth.webbrowser, "open", browser)


@pytest.mark.parametrize("action", ["send", "timeout", "ban", "is_moderator"])
@pytest.mark.parametrize("login_again", [False, True], ids=["logout", "new-account"])
def test_channel_action_does_not_cross_account_sessions(monkeypatch, action, login_again):
    local_browser(monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def delayed_live_page(channel):
        entered.set()
        assert release.wait(5)
        return VIDEO, "Synthetic live stream"

    # Preserve find_live_video's real worker-thread boundary.
    monkeypatch.setattr(chat_youtube, "live_now", delayed_live_page)

    async def run():
        account, api = account_and_api()
        target = ChatMessage("youtube", CHANNEL, "Target", "message", user_id="TARGET")
        args = {"send": (CHANNEL, "Intended as account A"), "timeout": (target, 30),
                "ban": (target,), "is_moderator": (CHANNEL,)}
        pending = asyncio.create_task(getattr(account, action)(*args[action]))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            account.logout()
            if login_again:
                assert (await account.login())["user_id"] == "B"
                account._cache[("ban", CHANNEL, "TARGET")] = "B_SESSION_BAN"
            fresh_cache = dict(account._cache)
            release.set()
            with pytest.raises(oauth.ApiError) as error:
                await asyncio.wait_for(pending, 3)
            assert error.value.status == 401
            # The old action must neither call the API nor refill the new cache.
            assert all(url.endswith("/channels") for _, url, _ in api.calls)
            assert account._cache == fresh_cache
            assert account.user_id == ("B" if login_again else "")
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(run())


def test_youtube_actions_succeed_in_the_original_session(monkeypatch):
    lookups = []

    def live_page(channel):
        lookups.append(channel)
        return VIDEO, "Synthetic live stream"

    monkeypatch.setattr(chat_youtube, "live_now", live_page)

    async def run():
        account, api = account_and_api()
        target = ChatMessage("youtube", CHANNEL, "Target", "message", user_id="TARGET")
        await account.send(CHANNEL, "hello")
        await account.timeout(target, 30)
        await account.unban(target)
        await account.ban(target)
        assert await account.is_moderator(CHANNEL) is True
        assert len(lookups) == 1  # the completed lookup is still reusable
        assert all(kwargs["headers"]["Authorization"] == "Bearer SYNTHETIC_A"
                   for _, _, kwargs in api.calls)
        sent = [kwargs["json"]["snippet"] for method, url, kwargs in api.calls
                if method == "POST" and url.endswith("/messages")]
        assert sent == [{"liveChatId": "CHAT", "type": "textMessageEvent",
                         "textMessageDetails": {"messageText": "hello"}}]
        bans = [kwargs["json"]["snippet"] for method, url, kwargs in api.calls
                if method == "POST" and url.endswith("/bans")]
        assert bans == [{"liveChatId": "CHAT", "type": "temporary",
                         "bannedUserDetails": {"channelId": "TARGET"}, "banDurationSeconds": 30},
                        {"liveChatId": "CHAT", "type": "permanent",
                         "bannedUserDetails": {"channelId": "TARGET"}}]
        assert any(method == "DELETE" and kwargs["params"] == {"id": "BAN"}
                   for method, _, kwargs in api.calls)
        assert account._cache[("ban", CHANNEL, "TARGET")] == "BAN"

    asyncio.run(run())

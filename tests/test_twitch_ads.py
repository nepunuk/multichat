# SPDX-License-Identifier: GPL-3.0-or-later
"""Use Streamlink's real Twitch reader/writer, without downloading any segments."""
import logging
import os
import threading
from types import SimpleNamespace

import pytest
import streamlink
from streamlink.plugins.twitch import TwitchHLSStream

from multichat.video import streams


@pytest.fixture
def readers(monkeypatch):
    opened = []
    session_type = streamlink.Streamlink

    def make():
        source = TwitchHLSStream(session_type(), "https://example.invalid/live.m3u8")
        source.disable_ads = True  # Streamlink 7; newer Twitch streams always filter ads.
        reader = source.__reader__(source)
        # Start empty threads so real close()/join() also works in Streamlink 7.
        # Segment processing below uses the real writer directly, without HTTP.
        monkeypatch.setattr(reader.worker, "run", lambda: None)
        monkeypatch.setattr(reader.writer, "run", lambda: None)
        reader.open()
        monkeypatch.setattr(reader.writer, "_write", lambda *args: reader.buffer.write(b"content"))
        opened.append(reader)
        return reader

    yield make
    for reader in opened:
        reader.close()


def write_segment(reader, *, ad):
    segment = SimpleNamespace(ad=ad, num=1, discontinuity=False)
    response = SimpleNamespace(raw=SimpleNamespace(drain_conn=lambda: None))
    reader.writer.write(segment, response)


def test_ad_state_follows_only_its_own_twitch_writer(readers):
    first, second = readers(), readers()
    alpha, beta = streams.AdWatch(first), streams.AdWatch(second)
    assert not alpha.active and not beta.active

    # Pre-roll: the real writer discards this before any content reaches the buffer.
    write_segment(first, ad=True)
    assert first.buffer.length == 0
    assert alpha.active and not beta.active

    write_segment(first, ad=False)
    assert first.buffer.length > 0
    assert not alpha.active and not beta.active

    # A later ad break can start and finish independently in the other channel.
    write_segment(second, ad=True)
    assert not alpha.active and beta.active
    write_segment(first, ad=True)
    assert alpha.active and beta.active
    write_segment(second, ad=False)
    assert alpha.active and not beta.active
    first.close()
    assert not alpha.active


def test_twitch_transport_keeps_preroll_and_restart_state_per_reader(monkeypatch, readers):
    sources = []
    stopped = []
    logger = logging.getLogger("streamlink")
    handlers, level = list(logger.handlers), logger.level

    class Plugin:
        def __init__(self, session, url, options):
            self.reader = readers()
            sources.append(self.reader)
            done = threading.Event()
            stopped.append(done)
            real_close = self.reader.close

            def close():
                done.set()
                real_close()

            def read(size):
                done.wait(5)
                return b""

            monkeypatch.setattr(self.reader, "close", close)
            monkeypatch.setattr(self.reader, "read", read)

        def streams(self):
            def open_stream():
                if len(sources) == 1:
                    write_segment(self.reader, ad=True)
                return self.reader

            return {"720p": SimpleNamespace(open=open_stream)}

        def get_title(self):
            return "Local fixture"

    class Session:
        def resolve_url(self, url):
            return "twitch", Plugin, url

    monkeypatch.setattr(streamlink, "Streamlink", Session)
    opened = []
    try:
        alpha = streams._twitch("alpha", 720)
        opened.append(alpha)
        beta = streams._twitch("beta", 720)
        opened.append(beta)
        assert alpha.ads.active and not beta.ads.active
        logging.getLogger("streamlink.plugins.twitch").warning("Detected advertisement break of 90 seconds")
        assert alpha.ads.active and not beta.ads.active
        assert logger.handlers == handlers and logger.level == level

        write_segment(sources[0], ad=False)
        write_segment(sources[1], ad=True)
        assert not alpha.ads.active and beta.ads.active
        beta.closer()
        assert not beta.ads.active

        restarted = streams._twitch("beta", 720)
        opened.append(restarted)
        assert not restarted.ads.active
        sources[1].pause()  # A late event on the retired reader cannot affect its replacement.
        assert not beta.ads.active and not restarted.ads.active
        write_segment(sources[2], ad=True)
        assert restarted.ads.active and not alpha.ads.active
    finally:
        for result in opened:
            result.closer()
            os.close(result.pipe_fd)
        assert all(done.is_set() for done in stopped)
    assert logger.handlers == handlers and logger.level == level


def test_reader_without_filter_support_does_not_report_ads():
    watch = streams.AdWatch(SimpleNamespace(closed=False))
    assert not watch.active
    watch.close()
    assert not watch.active


def test_open_failure_does_not_change_global_logging(monkeypatch):
    logger = logging.getLogger("streamlink")
    handlers, level = list(logger.handlers), logger.level

    def fail():
        raise OSError("fixture open failure")

    plugin = SimpleNamespace(streams=lambda: {"720p": SimpleNamespace(open=fail)}, get_title=lambda: "")
    session = SimpleNamespace(resolve_url=lambda url: ("twitch", lambda *args, **kwargs: plugin, url))
    monkeypatch.setattr(streamlink, "Streamlink", lambda: session)
    with pytest.raises(RuntimeError, match="fixture open failure"):
        streams._twitch("alpha", 720)
    assert logger.handlers == handlers and logger.level == level

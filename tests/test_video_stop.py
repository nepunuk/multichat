# SPDX-License-Identifier: GPL-3.0-or-later
"""Manual video stop survives layout changes, while a new start remains possible."""
from collections import deque
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QToolButton, QWidget

from multichat.core.i18n import _
from multichat.video import streams, tiles


class LocalPlayerWidget(QWidget):
    """Only the native decoder is replaced; VideoPanel/VideoTile stay real Qt widgets."""
    clicked = pyqtSignal()

    def __init__(self, *args, **kwargs):
        super().__init__()
        self._pending = None
        self.commands = []
        self.player = SimpleNamespace(command_async=lambda *args: self.commands.append(args), idle_active=False)
        self.stalls = deque()
        self.video_height = 720
        self.last_error = ""
        self.played = []

    def play(self, url, **options):
        self.played.append((url, options))

    def shutdown(self):
        pass


ENTRY = [("twitch:alpha", "twitch", "alpha")]


@pytest.fixture
def panel(monkeypatch, _session_qapplication):
    monkeypatch.setattr(tiles, "MpvWidget", LocalPlayerWidget)
    monkeypatch.setattr(tiles, "load_mpv", lambda: True)
    resolved = []

    def runner(coro, *, then, **kwargs):
        coro.close()  # No remote source resolver or downloader runs.
        resolved.append(True)
        then(("ok", streams.Stream("https://example.invalid/local-test-stream")))

    widget = tiles.VideoPanel(runner, {}, tiles.AudioHub())
    widget.show()
    widget.set_entries(ENTRY)
    assert widget.tiles["twitch:alpha"].state == "playing"
    assert len(resolved) == 1
    try:
        yield widget, resolved
    finally:
        widget.shutdown()
        widget.close()


def test_manual_stop_survives_tab_roundtrip_and_unchanged_layout_updates(panel):
    widget, resolved = panel
    tile = widget.tiles["twitch:alpha"]
    tile.stop_btn.click()
    assert tile.state == "idle"

    widget.set_entries(ENTRY)
    widget.set_entries(ENTRY, shown=False)
    widget.set_entries(ENTRY, shown=True)
    assert tile.state == "idle" and len(resolved) == 1
    assert not tile.retry.isActive() and not tile.watch.isActive() and not tile.auto_timer.isActive()

    # A second source appearing in this layout must not restart the first one.
    entries = ENTRY + [("kick:beta", "kick", "beta")]
    widget.set_entries(entries)
    widget.set_entries(entries)
    assert tile.state == "idle" and len(resolved) == 2
    assert widget.tiles["kick:beta"].state == "playing"


def test_changing_quality_keeps_manual_stop_and_applies_on_explicit_start(panel):
    widget, resolved = panel
    tile = widget.tiles["twitch:alpha"]
    tile.stop_btn.click()
    tile.qbox.setCurrentIndex(tile.qbox.findData("360p"))

    assert tile.quality == "360p" and tile.state == "idle" and len(resolved) == 1
    tile.stop_btn.click()
    assert tile.state == "playing" and tile.level() == "360p" and len(resolved) == 2


@pytest.mark.parametrize("action", ["play", "reload"])
def test_explicit_play_or_reload_clears_manual_stop(panel, action):
    widget, resolved = panel
    tile = widget.tiles["twitch:alpha"]
    tile.stop_btn.click()
    if action == "play":
        tile.stop_btn.click()
    else:
        button = next(b for b in tile.findChildren(QToolButton) if b.toolTip() == _("Reload stream"))
        button.click()

    assert tile.state == "playing" and len(resolved) == 2
    widget.set_entries(ENTRY, shown=False)
    widget.set_entries(ENTRY, shown=True)
    assert tile.state == "playing" and len(resolved) == 2


def test_changed_source_restarts_a_manually_stopped_tile(panel):
    widget, resolved = panel
    tile = widget.tiles["twitch:alpha"]
    tile.stop_btn.click()
    tile.set_source("beta")
    assert tile.value == "beta" and tile.state == "playing" and len(resolved) == 2
    tile.set_source(" beta ")
    assert len(resolved) == 2


def test_removing_channel_clears_manual_stop_before_retarget_reuses_tile(panel):
    widget, resolved = panel
    tile = widget.tiles["twitch:alpha"]
    tile.stop_btn.click()
    widget.forget(set())
    assert tile in widget.retired.values() and not tile._manually_stopped

    widget.set_entries([("twitch:beta", "twitch", "beta")])
    assert widget.tiles["twitch:beta"] is tile
    assert tile.value == "beta" and tile.state == "playing" and len(resolved) == 2


@pytest.mark.parametrize("manually_stopped", [False, True])
@pytest.mark.parametrize("action", ["disconnect_group", "disconnect_all"])
def test_reconnect_starts_after_disconnect_even_if_previously_manually_stopped(panel, action, manually_stopped):
    widget, resolved = panel
    tile = widget.tiles["twitch:alpha"]
    if manually_stopped:
        tile.stop_btn.click()
    widget.set_entries([])
    if action == "disconnect_group":
        widget.stop_hidden(set())
    else:
        widget.stop_all()
    assert tile.state == "idle"

    widget.set_entries(ENTRY)
    assert tile.state == "playing" and len(resolved) == 2


def test_background_timeout_does_not_turn_into_manual_stop(panel, _session_qapplication):
    widget, resolved = panel
    tile = widget.tiles["twitch:alpha"]
    widget.set_entries(ENTRY, shown=False)
    assert tile.state == "playing" and tile.idle_stop.isActive()
    tile.idle_stop.start(0)  # Advance the hidden timeout through the real Qt timer.
    QTest.qWait(10)
    _session_qapplication.processEvents()
    assert tile.state == "idle" and not tile.idle_stop.isActive()

    widget.set_entries(ENTRY)
    assert tile.state == "playing" and len(resolved) == 2


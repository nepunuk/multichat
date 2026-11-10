# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Corrgan
"""Video tiles: each channel group has its own column of tiles next to its chat."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import threading
import time

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QSizePolicy, QSlider, QStackedWidget, QStyle, QToolButton, QVBoxLayout, QWidget,
)

from ..core.constants import BADGES, NAMES, PLATFORMS
from ..core.i18n import _
from ..ui.widgets import CopyLabel
from . import player
from .player import DEFAULT_UA, MpvWidget, load_mpv
from .auto_quality import AutoQuality
from .streams import AUTO, DEFAULT_QUALITY, QUALITIES, Offline, normalize_quality, resolve

log = logging.getLogger("multichat.video")


OFFLINE_RETRY = 60
ERROR_RETRY = 30
ENDED_RETRY = 10
RECHECK_AFTER = OFFLINE_RETRY  # seconds: before this, an offline tile shown again waits for its retry timer
HIDDEN_STOP = 300  # seconds a hidden tile (other group) keeps streaming before it is stopped
AUTO_POLL_MS = 2000  # how often a tile set to "Automatic" looks at its stalls


class VideoTile(QFrame):
    audio_requested = pyqtSignal(object)
    quality_changed = pyqtSignal()  # also emitted when the volume changes (→ settings are saved)

    def __init__(self, platform: str, name: str, color: str, quality: str, runner, volume: int = 100,
                 key: str = "", channel: str = "") -> None:
        super().__init__()
        self.platform = platform
        self.key = key or platform  # "twitch:somechannel"
        self.runner = runner
        self.value = ""
        self.quality = normalize_quality(quality)
        self.auto = AutoQuality()  # the level while the quality is "Automatic"
        self.gen = 0
        self.state = "idle"
        self._manually_stopped = False
        self.muted = True
        self._mute_sent = False  # mute state not yet passed to mpv
        self._started = 0.0  # time.monotonic() of the last start()
        self._closer = None  # stops the running Streamlink data stream
        self._ads = None
        self._title = ""
        self._stall_from: float | None = None  # mpv has been waiting for data since then
        self.setFrameShape(QFrame.Shape.StyledPanel)

        style = self.style()
        self.icon_on = style.standardIcon(QStyle.StandardPixmap.SP_MediaVolume)
        self.icon_off = style.standardIcon(QStyle.StandardPixmap.SP_MediaVolumeMuted)

        self._name, self._color = name, color
        title = self.title = QLabel()
        self._set_title(channel)
        self.status = CopyLabel("–")
        self.status.setStyleSheet("color:#888")
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.qbox = QComboBox()
        for key, _height, label in QUALITIES:
            self.qbox.addItem(_(label), key)
        self.qbox.setCurrentIndex(max(0, self.qbox.findData(self.quality)))
        self.qbox.setToolTip(_("Quality"))
        self._show_auto_level()
        self.qbox.currentIndexChanged.connect(lambda _i: self._on_quality(self.qbox.currentData()))
        self.audio_btn = QToolButton()
        self.audio_btn.setIcon(self.icon_off)
        self.audio_btn.setToolTip(_("Sound of this tile on/off (the others are muted)"))
        self.audio_btn.clicked.connect(lambda: self.audio_requested.emit(self))
        self.volume = max(0, min(100, int(volume)))
        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(self.volume)
        self.vol.setFixedWidth(80)
        self.vol.setToolTip(_("Volume: {percent} %").format(percent=self.volume))
        self.vol.valueChanged.connect(self._on_volume)
        self.vol.sliderReleased.connect(self.quality_changed)  # save once, not on every step
        reload_btn = QToolButton()
        reload_btn.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        reload_btn.setToolTip(_("Reload stream"))
        reload_btn.clicked.connect(self.start)
        self.icon_stop = style.standardIcon(QStyle.StandardPixmap.SP_MediaStop)
        self.icon_play = style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        self.stop_btn = QToolButton()
        self.stop_btn.setIcon(self.icon_stop)
        self.stop_btn.setToolTip(_("Stop / restart this stream"))
        self.stop_btn.clicked.connect(self.toggle_stop)

        head = QHBoxLayout()
        head.setContentsMargins(8, 4, 6, 2)
        head.setSpacing(8)
        head.addWidget(title)
        head.addWidget(self.status, 1)
        head.addWidget(self.qbox)
        head.addWidget(self.audio_btn)
        head.addWidget(self.vol)
        head.addWidget(reload_btn)
        head.addWidget(self.stop_btn)

        self.message = QLabel()
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setWordWrap(True)
        self.message.setStyleSheet("background:#000; color:#777; font-size:13px")
        self.video = MpvWidget(platform, ytdl=platform == "youtube")
        self._apply_volume()
        self.video.clicked.connect(lambda: self.audio_requested.emit(self))
        self.stack = QStackedWidget()
        self.stack.addWidget(self.message)
        self.stack.addWidget(self.video)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(head)
        lay.addWidget(self.stack, 1)

        self.retry = QTimer(self, singleShot=True)
        self.retry.timeout.connect(self.start)
        self.watch = QTimer(self, interval=5000)
        self.watch.timeout.connect(self._check)
        self.auto_timer = QTimer(self, interval=AUTO_POLL_MS)
        self.auto_timer.timeout.connect(self._auto_check)
        self._show_message(_("Not connected yet"))

    def _set_title(self, channel: str) -> None:
        text = f"<b style='color:{self._color}'>{self._name}</b>"
        if channel:
            text += f" <span style='color:#aaa'>{html.escape(channel[:32])}</span>"
        self.title.setText(text)

    def retarget(self, key: str, channel: str, quality: str, volume: int) -> None:
        """Reuses this (stopped) tile for another channel of the same platform – creating and
        destroying mpv players blocks the window (~0.3 s each with GPU decoding)."""
        self.key = key
        self.value = ""
        self._manually_stopped = False
        self._set_title(channel)
        self.quality = normalize_quality(quality)
        self.auto.reset()
        self.qbox.blockSignals(True)
        self.qbox.setCurrentIndex(max(0, self.qbox.findData(self.quality)))
        self.qbox.blockSignals(False)
        self._show_auto_level()
        self.vol.blockSignals(True)
        self.vol.setValue(max(0, min(100, int(volume))))
        self.vol.blockSignals(False)
        self._on_volume(self.vol.value())
        self.set_muted(True)
        self._show_message(_("Not connected yet"))

    # ---- Control ---------------------------------------------------------- #
    def set_source(self, value: str) -> None:
        """Called on every change in the window (tab switch, connect, …). A waiting tile
        (offline, ended) checks again only if its last attempt is a while ago – otherwise its
        retry timer does it (the log showed offline channels checked every few seconds)."""
        value = value.strip()
        self.setVisible(bool(value))
        if value == self.value and self._manually_stopped:
            return
        waiting = self.state in ("offline", "ended") and time.monotonic() - self._started > RECHECK_AFTER
        if value != self.value or self.state == "idle" or waiting:
            if value != self.value:
                self.auto.reset(self._tile_pixels())  # another channel starts over
                self._show_auto_level()
            self.value = value
            self.start() if value else self.stop()

    def _release(self) -> None:
        """Stop the running Streamlink data stream (and close pipes that were never loaded)."""
        if self._closer:
            # closing Streamlink waits for its download threads – in the background, or the
            # window freezes for a moment on every disconnect
            closer = self._closer

            def close() -> None:
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass

            threading.Thread(target=close, name=f"close-{self.platform}", daemon=True).start()
        self._closer = None
        self._ads = None
        pending = self.video._pending
        if pending and pending[0].startswith("fdclose://"):
            try:
                os.close(int(pending[0].split("//", 1)[1]))
            except (OSError, ValueError):
                pass
        self.video._pending = None

    def start(self) -> None:
        self._manually_stopped = False
        self.retry.stop()
        self.watch.stop()
        self.auto_timer.stop()
        self._release()
        self.gen += 1
        if not self.value:
            return
        self._started = time.monotonic()
        gen = self.gen
        self.state = "resolving"
        self.stop_btn.setIcon(self.icon_stop)
        self.set_status(_("looking for stream …"))
        if self.stack.currentIndex() == 0:
            self._show_message(_("Looking for stream …"))
        self.runner(self._resolve(), then=lambda res: self._resolved(gen, res))

    def stop(self, *, manual: bool = False) -> None:
        # Layout updates preserve a toolbar stop; a disconnect ends that intent
        # so reconnecting the channel can start this tile again.
        self._manually_stopped = manual
        self.gen += 1
        self.retry.stop()
        self.watch.stop()
        self.auto_timer.stop()
        self.state = "idle"
        self._release()
        try:
            self.video.player.command_async("stop")  # doesn't wait for mpv
        except Exception:  # noqa: BLE001
            pass
        self.stop_btn.setIcon(self.icon_play)
        self.set_status(_("stopped"))
        self._show_message(_("Stopped – ▶ starts the stream again"))
        log.info("Video %s: stopped", self.platform)

    def toggle_stop(self) -> None:
        if self.state == "idle":
            self.start()
        else:
            self.stop(manual=True)

    async def _resolve(self):
        try:
            return "ok", await asyncio.to_thread(resolve, self.platform, self.value, self.level())
        except Offline:
            return "offline", None
        except Exception as e:  # noqa: BLE001
            # "not found", Streamlink and yt-dlp errors carry their own message – a traceback
            # only for the unexpected ones
            log.warning("Video %s %r: %s", self.platform, self.value, e,
                        exc_info=not isinstance(e, (RuntimeError, ValueError)))
            return "error", str(e)

    def _resolved(self, gen: int, res) -> None:
        kind, data = res
        if gen != self.gen:  # restarted in the meantime → discard the result
            if kind == "ok" and (data.closer or data.pipe_fd is not None):
                # Closing Streamlink waits for its download threads – not on the UI thread
                # (quick channel changes would make the window hang).
                def discard(closer=data.closer, fd=data.pipe_fd) -> None:
                    for f in (closer, lambda: fd is not None and os.close(fd)):
                        try:
                            f and f()
                        except Exception:  # noqa: BLE001
                            pass

                threading.Thread(target=discard, name=f"discard-{self.platform}", daemon=True).start()
            return
        log.info("Video %s: %s%s", self.platform, kind, f" – {data}" if kind == "error" else "")
        if kind == "ok":
            headers = {k: v for k, v in (data.headers or {}).items() if k.lower() not in ("accept-encoding",)}
            # options for this stream only, sent along with loadfile (nothing waits for mpv)
            opts = {"user-agent": headers.pop("User-Agent", DEFAULT_UA)}
            fields = [f"{k}: {v}" for k, v in headers.items() if "," not in v]
            if fields:
                opts["http-header-fields"] = fields
            if data.audio_url:
                opts["audio-file"] = data.audio_url
            if data.ytdl_format:
                opts["ytdl-format"] = data.ytdl_format
            self.stack.setCurrentIndex(1)
            self._closer, self._ads, self._title = data.closer, data.ads, data.title
            self.video.play(data.url, **opts)
            self.state = "playing"
            self.set_status(_("live: {title}").format(title=data.title) if data.title else _("live"))
            self.watch.start()
            self.auto.loaded(data.heights, data.height)
            self._stall_from = None
            self.auto_timer.start()
            return
        delay = OFFLINE_RETRY if kind == "offline" else ERROR_RETRY
        nxt = time.strftime("%H:%M", time.localtime(time.time() + delay))
        text = _("offline") if kind == "offline" else _("Error: {error}").format(error=data)
        self.state = "offline"
        self.set_status(text)
        self._show_message(f"{text}\n\n" + _("next attempt at {time}").format(time=nxt))
        self.retry.start(delay * 1000)

    def _check(self) -> None:
        if self.state != "playing":
            return
        if self._ads is not None:  # Twitch: show skipped ads instead of a silent still image
            ad_text = _("live – skipping ad break, back in a moment …")
            if self._ads.active:
                if self.status.full != ad_text:
                    log.info("Video %s: skipping ad break", self.platform)
                self.set_status(ad_text)
            elif self.status.full == ad_text:
                self.set_status(_("live: {title}").format(title=self._title) if self._title else _("live"))
        try:
            idle = self.video.player.idle_active
        except Exception:  # noqa: BLE001
            idle = True
        if idle and self.video._pending:
            return  # loads once the tile is first shown (needs its OpenGL context) – not ended
        if idle:
            self.watch.stop()
            err = self.video.last_error
            if err:  # yt-dlp (via mpv) failed
                self.state = "offline"
                nxt = time.strftime("%H:%M", time.localtime(time.time() + ERROR_RETRY))
                log.warning("Video %s: yt-dlp/mpv error: %s", self.platform, err)
                self.set_status(_("Error: {error}").format(error=err))
                self._show_message(_("Error: {error}").format(error=err) + "\n\n" + _("next attempt at {time}").format(time=nxt))
                self.retry.start(ERROR_RETRY * 1000)
                return
            self.state = "ended"
            log.info("Video %s: playback ended", self.platform)
            self.set_status(_("stream ended"))
            self._show_message(_("Stream ended – checking again …"))
            self.retry.start(ENDED_RETRY * 1000)

    def _on_quality(self, q: str) -> None:
        self.quality = q
        if q == AUTO:
            self.auto.reset(self._tile_pixels())
        self._show_auto_level()
        self.quality_changed.emit()
        if self.value and not self._manually_stopped:
            self.start()

    # ---- Quality "Automatic" (the rules are in auto_quality.py) ------------- #
    def level(self) -> str:
        """The quality level streams are looked up with."""
        return self.auto.level if self.quality == AUTO else self.quality

    def _tile_pixels(self) -> int:
        """Height of the visible picture in device pixels, 0 while hidden."""
        video = self.video
        return round(video.height() * video.devicePixelRatioF()) if video.isVisible() else 0

    def _show_auto_level(self) -> None:
        index = self.qbox.findData(AUTO)
        if index < 0:
            return
        if self.quality == AUTO:
            label = next(_(label) for key, _h, label in QUALITIES if key == self.auto.level)
            self.qbox.setItemText(index, _("Automatic ({level})").format(level=label))
        else:
            self.qbox.setItemText(index, _("Automatic"))

    def _auto_check(self) -> None:
        """Every two seconds while playing: what mpv reported about waiting for data. Only
        looks at values mpv has already handed over – no call into the player."""
        if self.state != "playing":
            self.auto_timer.stop()
            return
        stalls = self.video.stalls
        while stalls:
            at, stalled = stalls.popleft()
            self.auto.stall(stalled, at)
            if stalled and self._stall_from is None:
                self._stall_from = at
            elif not stalled and self._stall_from is not None:
                # in the verbose log for every quality: shows whether the line keeps up
                log.debug("Video %s: waited %.1f s for data (%s)", self.platform, at - self._stall_from, self.level())
                self._stall_from = None
        if self.quality != AUTO:
            return
        target = self.auto.poll(ads_active=bool(self._ads is not None and self._ads.active),
                                played_height=self.video.video_height, tile_height=self._tile_pixels())
        self._show_auto_level()  # also when a level turned out to be the stream's best
        if target:
            log.info("Video %s: automatic quality %s", self.platform, self.auto.reason)
            self.start()

    def _on_volume(self, value: int) -> None:
        self.volume = value
        self.vol.setToolTip(_("Volume: {percent} %").format(percent=value))
        self._apply_volume()
        if not self.vol.isSliderDown():  # wheel/keyboard: save right away
            self.quality_changed.emit()

    def _apply_volume(self) -> None:
        try:  # async: a synchronous property set waits for mpv, which may be busy stopping
            self.video.player.command_async("set", "volume", str(self.volume))
        except Exception:  # noqa: BLE001
            pass

    def set_muted(self, muted: bool) -> None:
        if muted == self.muted and self._mute_sent:
            return  # unchanged – every mpv call waits for the player, which may be busy stopping
        self.muted = muted
        self._mute_sent = True
        try:
            self.video.player.command_async("set", "mute", "yes" if muted else "no")  # doesn't wait for mpv
        except Exception:  # noqa: BLE001
            pass
        self.audio_btn.setIcon(self.icon_off if muted else self.icon_on)
        self.setStyleSheet("" if muted else "VideoTile { border: 2px solid #4a9eff; }")

    def set_status(self, text: str) -> None:
        self.status.set_full(text)

    def _show_message(self, text: str) -> None:
        self.message.setText(text)
        self.stack.setCurrentIndex(0)

    def shutdown(self) -> None:
        self.gen += 1
        self.retry.stop()
        self.watch.stop()
        self.auto_timer.stop()
        self._release()
        self.video.shutdown()


class AudioHub(QObject):
    """Sound across all groups: only one tile in the whole window plays sound."""

    changed = pyqtSignal()

    def __init__(self, audio: str | None = None) -> None:
        super().__init__()
        self.audio = audio  # source key of the tile with sound
        self.owner: VideoPanel | None = None  # the group whose tile it is (same channel in two groups)
        self.panels: list[VideoPanel] = []

    def toggle(self, panel: "VideoPanel", key: str) -> None:
        if self.audio == key and self.owner in (None, panel):
            self.audio, self.owner = None, None
        else:
            self.audio, self.owner = key, panel
        self.apply()
        self.changed.emit()

    def apply(self) -> None:
        if self.owner not in self.panels or not (self.owner.shown and self.audio in self.owner.tiles):
            # e.g. after a start: the first shown group with that channel gets the sound
            self.owner = next((pn for pn in self.panels if pn.shown and self.audio in pn.tiles), self.owner)
        for pn in self.panels:
            pn.apply_audio(self.audio if pn is self.owner else None)


class VideoPanel(QWidget):
    """One group's video tiles, one below the other.

    A tile is created once per channel and kept: hiding the group only hides its tiles
    (creating and destroying mpv players made every switch hang). The panel never changes
    its parent – moving an OpenGL widget to another parent makes Qt recreate its context."""

    changed = pyqtSignal()

    def __init__(self, runner, config: dict, hub: AudioHub) -> None:
        super().__init__()
        self.config = config
        self.runner = runner
        self.hub = hub
        hub.panels.append(self)
        self.tiles: dict[str, VideoTile] = {}
        self.retired: dict[str, VideoTile] = {}  # platform → stopped tile, reused for the next channel
        self.shown = False  # the group is on screen
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(6)
        self._ok = bool(load_mpv())
        self.setMinimumWidth(320)

        if not self._ok:
            hint = QLabel(
                "<b>" + _("Videos need libmpv.") + "</b><br><br>"
                "Ubuntu/Debian: <code>sudo apt install libmpv2</code><br>"
                "Fedora: <code>sudo dnf install mpv-libs</code><br>"
                "Arch: <code>sudo pacman -S mpv</code><br><br>"
                f"<span style='color:#888'>{player.MPV_ERROR}</span>"
            )
            hint.setWordWrap(True)
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.lay.addWidget(hint)
            return

        self.empty = QLabel(_("Enter channels on the left and click “Connect” –\nthe streams will then appear here."))
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet("color:#888")
        self.lay.addWidget(self.empty, 1)

    @property
    def available(self) -> bool:
        return self._ok

    def _setting(self, name: str, key: str, platform: str, default):
        values = self.config.get(name) or {}
        return values.get(key, values.get(platform, default))

    def _tile(self, key: str, platform: str, channel: str) -> VideoTile:
        tile = self.tiles.get(key)
        if tile is None and platform in self.retired:
            tile = self.retired.pop(platform)
            tile.retarget(key, channel, self._setting("quality", key, platform, DEFAULT_QUALITY),
                          self._setting("volume", key, platform, 100))
            self.tiles[key] = tile
        if tile is None:
            tile = VideoTile(platform, NAMES[platform], BADGES[platform][0],
                             self._setting("quality", key, platform, DEFAULT_QUALITY), self.runner,
                             volume=self._setting("volume", key, platform, 100), key=key, channel=channel)
            tile.idle_stop = QTimer(tile, singleShot=True)
            tile.idle_stop.timeout.connect(tile.stop)
            tile.audio_requested.connect(lambda t: self.hub.toggle(self, t.key))
            tile.quality_changed.connect(self._remember)
            self.tiles[key] = tile
            self.lay.addWidget(tile, 1)
            # same order as the channel rows (Twitch, Kick, YouTube) – moving within the
            # layout keeps the parent
            for i, t in enumerate(sorted(self.tiles.values(), key=lambda t: PLATFORMS.index(t.platform))):
                if self.lay.indexOf(t) != i + 1:  # 0 is the "enter channels" hint
                    self.lay.removeWidget(t)
                    self.lay.insertWidget(i + 1, t, 1)
        return tile

    def set_entries(self, entries, shown: bool = True) -> None:
        """entries: [(key, platform, channel)] of the group's connected channels with video.
        Tiles not listed – or all, while the group is not on screen – are hidden but keep
        streaming muted: restarting a stream rebuilds mpv's video pipeline (~0.2 s per tile on
        the UI thread), so switching back would stutter. They stop after HIDDEN_STOP."""
        if not self._ok:
            return
        self.shown = shown
        wanted = {key: (platform, channel) for key, platform, channel in entries if channel} if shown else {}
        for key, tile in self.tiles.items():
            if key not in wanted and not tile.idle_stop.isActive() and tile.state != "idle":
                tile.idle_stop.start(HIDDEN_STOP * 1000)
            if key not in wanted:
                tile.set_muted(True)
                tile.hide()
        for key, (platform, channel) in wanted.items():
            tile = self._tile(key, platform, channel)
            tile.idle_stop.stop()
            tile.show()
            tile.set_source(channel)
        self.empty.setVisible(not wanted)
        self.hub.apply()

    def forget(self, keep: set[str]) -> None:
        """Tiles of channels the group doesn't have any more: stopped and kept for the next
        channel of that platform (destroying an mpv player blocks the window)."""
        for key in [k for k in self.tiles if k not in keep]:
            self._remember()
            tile = self.tiles.pop(key)
            tile.idle_stop.stop()
            if tile.state != "idle" or tile._manually_stopped:
                tile.stop()
            tile.set_muted(True)
            tile.hide()
            if tile.platform in self.retired:  # one per platform is enough (can't really happen)
                self.lay.removeWidget(tile)
                tile.shutdown()
                tile.deleteLater()
            else:
                self.retired[tile.platform] = tile

    def stop_all(self) -> None:
        for tile in self.tiles.values():
            tile.stop()

    def stop_hidden(self, keep: set[str]) -> None:
        """Stops tiles of channels that are no longer connected (instead of letting them run)."""
        for key, tile in self.tiles.items():
            if key not in keep and (tile.state != "idle" or tile._manually_stopped):
                tile.stop()

    def apply_audio(self, audio: str | None) -> None:
        for k, t in self.tiles.items():
            t.set_muted(k != audio or t.isHidden() or not self.shown)  # hidden groups stay silent

    def _remember(self) -> None:
        """Keeps quality and volume per channel, also for tiles that are removed later."""
        for name, attr in (("quality", "quality"), ("volume", "volume")):
            store = self.config.setdefault(name, {})
            for key, t in self.tiles.items():
                store.pop(key, None)  # re-added at the end: the newest entries are kept (config.MAX_REMEMBERED)
                store[key] = getattr(t, attr)
        self.changed.emit()

    def frame_counts(self) -> dict[str, tuple[int, float]]:
        """(frames drawn, ms spent drawing) per tile since the last call (performance log)."""
        out = {}
        for k, t in self.tiles.items():
            video = getattr(t, "video", None)
            if video is not None and getattr(video, "frames", 0):
                out[k] = (video.frames, video.paint_ms)
                video.frames, video.paint_ms = 0, 0.0
        return out

    def state(self) -> dict:
        quality = dict(self.config.get("quality") or {})
        volume = dict(self.config.get("volume") or {})
        for key, t in self.tiles.items():
            quality[key], volume[key] = t.quality, t.volume
        return {"quality": quality, "volume": volume}

    def shutdown(self) -> None:
        if self in self.hub.panels:
            self.hub.panels.remove(self)
        for tile in [*self.tiles.values(), *self.retired.values()]:
            tile.shutdown()

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Corrgan
"""Chat view: renders messages as HTML and loads emote images asynchronously.

Images are scaled to the line height once when they arrive (instead of on every paint),
kept in a bounded cache, and animated GIF/WebP emotes play via QMovie. Repaints and
re-layouts are coalesced with timers, so busy chats stay smooth.
"""
from __future__ import annotations

import html
import re
import time
from collections import OrderedDict

from PyQt6.QtCore import QBuffer, QByteArray, QObject, QPoint, QSize, Qt, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QImage, QMovie, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument
from PyQt6.QtWidgets import QTextBrowser, QToolButton

from ..chat import alerts
from ..chat.models import ChatMessage, user_key
from . import icons, image_io
from .image_io import ImageCache, ImageTransfers, raster_reader, read_scaled
from ..core.constants import BADGES, BROWSER_UA, FAVICONS
from ..core.i18n import _

EMOTE_H = 22  # emote height at the default font size
DEFAULT_FONT = 14
FONT_RANGE = (9, 40)
ROLE_ORDER = ("broadcaster", "moderator", "vip", "subscriber", "verified")
IMAGE_RES = QTextDocument.ResourceType.ImageResource.value
MAX_IMAGE_BYTES = 4 * 1024 * 1024  # emotes are a few KB – anything bigger is not an emote
MAX_CACHED = 1500  # distinct images kept in memory
MAX_CACHE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_ACTIVE = 8
MAX_IMAGE_WAITING = 96
MAX_MOVIES = 128
MAX_MOVIE_BYTES = 16 * 1024 * 1024  # encoded animation buffers across the view
MAX_MOVIE_WORKING_BYTES = 32 * 1024 * 1024  # charge source/scratch/scaled surfaces before creating QMovie
MAX_ANIMATION_FRAMES = 120
MAX_ANIMATION_PIXELS = 4 * 1024 * 1024  # source pixels across all declared frames
MAX_ANIMATION_FRAME_BYTES = 16 * 1024 * 1024
# Decoded frames are kept (QMovie.CacheAll) for ordinary emotes: decoding every frame again
# costs about three times the CPU of showing a cached one (measured with 12 emotes playing:
# 4-8 % of a core instead of 1.5-2.5 %). The cache has its own budget; animations that don't
# fit – long or large ones – are decoded frame by frame instead.
MAX_FRAME_CACHE_PER_MOVIE = 2 * 1024 * 1024  # an emote of 60 frames at 56 px height needs ~0.75 MiB
MAX_FRAME_CACHE_BYTES = 64 * 1024 * 1024  # across all animations of one view
MAX_FAILED = 1500  # negative cache holds URLs/deadlines, never decoded images
# An image that couldn't be loaded is tried again in the background: after 30 s, then at
# doubling intervals up to 10 minutes. Its lines keep showing the name meanwhile and are
# only touched again when the image has arrived (see _retry_images).
IMAGE_RETRY_SECONDS = 30
IMAGE_RETRY_MAX_SECONDS = 600
RETRYING = float("inf")  # "deadline" of a failed image whose next attempt is under way
RETRY_BATCH = 32  # spread recovery after a network outage over multiple GUI turns
FAILURE_NOTIFY_BATCH = 8
# Qt's HTML import creates a new image object + text format for every <img> (platform icon,
# role icons, emotes) and only a clear() releases them – ~2-3 KB per chat line that stayed
# in memory even after the line scrolled out. After this many inserted lines the view is
# rebuilt once (needs_compaction), which empties that list.
COMPACT_AFTER = 8000
MENTION_BG = "#4d1b26"  # lines that name you (the account you are logged in with)


def own_pattern(name: str):
    """Your name as a whole word, with or without @ ("@Me", "me" – not "meow", not "some")."""
    return re.compile(r"(?<!\w)@?" + re.escape(name) + r"(?!\w)", re.IGNORECASE)
MAX_ANIMATED = 64  # animations that play at the same time (the most recently shown ones)
FRAME_MS = 50  # repaint rate for animations (20 fps)
RELAYOUT_SCAN = 400  # lines checked for a newly loaded image


def _allowed_url(url: QUrl) -> bool:
    """https only (see image_io.allowed_image_url)."""
    return image_io.allowed_image_url(url)


class ChatView(QTextBrowser):
    """Chat view that loads emote images asynchronously."""

    rerender_needed = pyqtSignal()
    image_failed = pyqtSignal(str)  # an emote that couldn't be loaded – its lines show the name instead
    image_restored = pyqtSignal(str)  # a later attempt got it – its lines can show the image again
    name_context = pyqtSignal(QUrl)  # right-click on a user name ("msg:<uid>")
    name_clicked = pyqtSignal(QUrl)  # left click on a user name ("msg:<uid>")
    follow_changed = pyqtSignal(bool)  # True: sticks to the newest message

    def __init__(self) -> None:
        super().__init__()
        self._closed = False
        self.setOpenLinks(False)
        self.font_px = DEFAULT_FONT
        self.emote_h = EMOTE_H
        self.selected: tuple[str, str] | None = None  # (platform, user key) of the selected user
        self.alerts_cfg: dict = dict(alerts.DEFAULT_ALERTS)  # which events are highlighted
        # platform → (pattern, user ID, lower-case name) of the account you are logged in with
        self.own: dict[str, tuple] = {}
        self._icons: dict = {}
        self._apply_style()
        self._images = ImageTransfers(self, max_active=MAX_IMAGE_ACTIVE, max_waiting=MAX_IMAGE_WAITING,
                                      max_bytes=MAX_IMAGE_BYTES, user_agent=BROWSER_UA)
        self.nam = self._images.nam
        self._image_connections = [self._images.loaded.connect(self._image_data),
                                   self._images.failed.connect(self._failed_image)]
        self.cache = ImageCache()  # LRU with both a count and a byte ceiling
        self.movies: dict[str, tuple[QMovie, QBuffer]] = {}
        self._movie_bytes = 0
        self._movie_working_bytes = 0
        self._movie_costs: dict[str, int] = {}
        self._frame_cache: dict[str, int] = {}  # key → bytes of cached frames (CacheAll movies)
        self._frame_cache_bytes = 0
        self._movie_resume: dict[str, QTimer] = {}
        self._movie_connections: dict[str, list] = {}
        self._playing: set[str] = set()
        self.pending: set[str] = set()
        self.failed: OrderedDict[str, float] = OrderedDict()  # URL → time of the next attempt
        self._attempts: dict[str, int] = {}  # failed attempts in a row, for the URLs in `failed`
        self._failure_notifications: OrderedDict[str, None] = OrderedDict()
        self._failure_timer = QTimer(self, singleShot=True, interval=0)
        self._timer_connections = [self._failure_timer.timeout.connect(self._emit_image_failures)]
        self._image_retry = QTimer(self, singleShot=True)
        self._timer_connections.append(self._image_retry.timeout.connect(self._retry_images))
        self.placeholder = QImage(EMOTE_H, EMOTE_H, QImage.Format.Format_ARGB32)  # resized in set_font_px
        self.placeholder.fill(Qt.GlobalColor.transparent)
        self._relayout = QTimer(self, singleShot=True, interval=120)  # new images change line widths
        self._timer_connections.append(self._relayout.timeout.connect(self._do_relayout))
        self._repaint = QTimer(self, singleShot=True, interval=FRAME_MS)
        self._timer_connections.append(self._repaint.timeout.connect(self.viewport().update))
        self._loaded_keys: set[str] = set()  # images loaded since the last relayout
        self._anim = QTimer(self, singleShot=True, interval=200)  # which animations are on screen
        self._timer_connections.append(self._anim.timeout.connect(self.update_animations))

        # Auto-scroll: follow the newest message until the user scrolls up; scrolling back to
        # the bottom (or the jump button) follows again. Kept as state instead of checking the
        # position before each append – images loading later make the text taller, and a
        # position check would then wrongly count that as "scrolled up".
        self.follow = True
        self.held = False
        self.unseen = 0
        # References to the actual displayed messages, bounded by the owning group's
        # line limit. While paused they survive trimming of the shared live history.
        self.retained_messages: OrderedDict[int, ChatMessage] = OrderedDict()
        self._rebuild_anchor: tuple | None = None
        self.inserted = 0  # lines inserted since the last clear() (see COMPACT_AFTER)
        self._programmatic = 0
        bar = self.verticalScrollBar()
        bar.valueChanged.connect(self._on_scroll)
        bar.rangeChanged.connect(self._on_range)
        self.jump = QToolButton(self.viewport())
        self.jump.setCursor(Qt.CursorShape.PointingHandCursor)
        self.jump.setStyleSheet(
            "QToolButton { background:#9147ff; color:white; border:none; border-radius:12px;"
            " padding:4px 14px; font-weight:bold; }"
            "QToolButton:hover { background:#a970ff; }"
        )
        self.jump.clicked.connect(self.scroll_to_bottom)
        self.jump.hide()
        self._update_jump()

    def shutdown(self) -> None:
        """Stop terminal image work before this widget or its window is destroyed."""
        if self._closed:
            return
        self._closed = True
        for connection in self._image_connections + self._timer_connections:
            QObject.disconnect(connection)
        self._image_connections.clear()
        self._timer_connections.clear()
        for timer in (self._failure_timer, self._image_retry, self._relayout, self._repaint, self._anim):
            timer.stop()
        self._images.shutdown()
        for key in list(self.movies):
            self._drop_movie(key)
        self.pending.clear()
        self.failed.clear()
        self._attempts.clear()
        self._failure_notifications.clear()
        self._loaded_keys.clear()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.shutdown()
        super().closeEvent(event)

    # ---- Auto-scroll ------------------------------------------------------ #
    def _at_bottom(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4

    def _set_follow(self, on: bool) -> None:
        if on:
            self.unseen = 0
        if on != self.follow:
            self.follow = on
            self.follow_changed.emit(on)
        self._update_jump()

    def _on_scroll(self, _value: int) -> None:
        if self._closed:
            return
        self._anim.start()
        if not self._programmatic:  # moved by the user (wheel, scrollbar, keys)
            self._set_follow(self._at_bottom())

    def _on_range(self, _min: int, _max: int) -> None:
        if self._closed:
            return
        if self.follow:
            self.scroll_to_bottom()

    def scroll_to_bottom(self) -> None:
        self._programmatic += 1
        try:
            bar = self.verticalScrollBar()
            bar.setValue(bar.maximum())
        finally:
            self._programmatic -= 1
        self._set_follow(True)

    def rebuild(self, htmls: list) -> None:
        """Replaces all messages; keeps following, or keeps the position while scrolled up."""
        anchor = self._rebuild_anchor or self._read_anchor()
        self._rebuild_anchor = None
        self._programmatic += 1
        try:
            self.clear()
            self.inserted = 0
            self.append_many(htmls)
            if not self.follow:
                self._restore_anchor(anchor)
        finally:
            self._programmatic -= 1
        if self.follow:
            self.scroll_to_bottom()

    def retain_messages(self, messages, limit: int, *, replace: bool = False) -> None:
        if replace:
            self.retained_messages.clear()
        for message in messages:
            self.retained_messages[message.uid] = message
        while len(self.retained_messages) > limit:
            self.retained_messages.popitem(last=False)

    def reset_history(self) -> None:
        """Drop the paused model as well as its document when a group is cleared/reused."""
        # Removed groups stay alive for reuse. Their old replies and retries must
        # stop now, including failures that an aborted reply would otherwise emit.
        self._images.cancel_all()
        for timer in (self._failure_timer, self._image_retry, self._relayout):
            timer.stop()
        self.pending.clear()
        self.failed.clear()
        self._attempts.clear()
        self._failure_notifications.clear()
        self._loaded_keys.clear()
        self.retained_messages.clear()
        self._rebuild_anchor = None
        self.held = False
        self.rebuild([])
        self.scroll_to_bottom()
        self.unseen = 0
        self._update_jump()
        self.update_animations()  # cached movies stay reusable, but no longer visible

    def _read_anchor(self) -> tuple:
        """The character currently at the top, including its offset inside a wrapped line."""
        cur = self.cursorForPosition(QPoint(0, 0))
        block = cur.block()
        return (block.userState(), cur.position() - block.position(), self.cursorRect(cur).top(),
                self.verticalScrollBar().value())

    def _restore_anchor(self, anchor: tuple) -> None:
        uid, offset, top, fallback = anchor
        block = self.document().firstBlock()
        while uid >= 0 and block.isValid():
            if block.userState() == uid:
                cur = QTextCursor(block)
                cur.setPosition(block.position() + min(offset, block.length() - 1))
                bar = self.verticalScrollBar()
                bar.setValue(bar.value() + self.cursorRect(cur).top() - top)
                return
            block = block.next()
        self.verticalScrollBar().setValue(fallback)

    def compact(self) -> None:
        """Release obsolete Qt formats while preserving exactly the displayed messages.

        A paused view can contain older lines no longer in the shared history. Rebuilding
        from that history would replace the passage being read, so reimport this document.
        """
        doc = self.document()
        content = doc.toHtml()
        ids = []
        block = doc.firstBlock()
        while block.isValid():
            ids.append(block.userState())
            block = block.next()
        anchor = self._read_anchor()
        self._programmatic += 1
        try:
            self.setHtml(content)
            block = doc.firstBlock()
            for uid in ids:
                if not block.isValid():
                    break
                block.setUserState(uid)
                block = block.next()
            self.inserted = doc.blockCount() if not doc.isEmpty() else 0
            if not self.follow:
                self._restore_anchor(anchor)
        finally:
            self._programmatic -= 1
        if self.follow:
            self.scroll_to_bottom()

    def note_unseen(self, n: int) -> None:
        """Messages that arrived while scrolled up (shown once you return to the bottom)."""
        self.unseen += n
        self._update_jump()

    def _update_jump(self) -> None:
        if self.follow:
            self.jump.hide()
            return
        text = _("New messages") if self.unseen else _("Jump to newest")
        self.jump.setText(f"↓ {text}" + (f" ({self.unseen})" if self.unseen else ""))
        self.jump.adjustSize()
        vp = self.viewport()
        self.jump.move((vp.width() - self.jump.width()) // 2, vp.height() - self.jump.height() - 10)
        self.jump.show()
        self.jump.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.follow:
            self.scroll_to_bottom()
        self._update_jump()

    def loadResource(self, rtype, url: QUrl):  # noqa: N802
        if self._closed and int(getattr(rtype, "value", rtype)) == IMAGE_RES:
            return self.placeholder
        if int(getattr(rtype, "value", rtype)) == IMAGE_RES and url.scheme() == "mcicon":
            # drawn role/alert icons (emoji fonts can't be relied on)
            name = url.path()
            px = max(1, round(self.font_px * 1.1 * self.devicePixelRatioF()))
            key = ("mcicon", name, px)
            if key not in self._icons:
                img = icons.image(name, px) if name in icons.NAMES else self.placeholder
                img.setDevicePixelRatio(self.devicePixelRatioF())
                self._icons[key] = img
            return self._icons[key]
        if int(getattr(rtype, "value", rtype)) == IMAGE_RES and url.scheme() in ("http", "https"):
            key = url.toString()
            img = self.cache.get(key)
            if img is not None:
                return img
            if _allowed_url(url) and key not in self.pending and key not in self.failed:
                self.pending.add(key)
                if not self._images.request(key):
                    self._failed_image(key)
            return self.placeholder
        return super().loadResource(rtype, url)

    # ---- Images ---------------------------------------------------------- #
    def _target_size(self, size: QSize) -> QSize:
        """Size in device pixels for the line height (keeps the aspect ratio)."""
        h = min(256, max(1, round(self.emote_h * self.devicePixelRatioF())))
        w = max(1, round(size.width() * h / max(1, size.height())))
        return QSize(min(w, h * 6), h)

    def _scaled(self, img: QImage) -> QImage:
        out = img.scaled(self._target_size(img.size()), Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
        out.setDevicePixelRatio(self.devicePixelRatioF())
        return out

    def _failed_image(self, key: str) -> None:
        if self._closed:
            return
        self.pending.discard(key)
        known = self.failed.pop(key, None) is not None
        attempts = self._attempts[key] = self._attempts.get(key, 0) + 1
        self.failed[key] = time.monotonic() + min(IMAGE_RETRY_MAX_SECONDS, IMAGE_RETRY_SECONDS * 2 ** (attempts - 1))
        while len(self.failed) > MAX_FAILED:
            old, _deadline = self.failed.popitem(last=False)
            self._attempts.pop(old, None)
        self._schedule_image_retry()
        if known:
            return  # a background retry failed again: the lines already show the name
        # loadResource runs inside Qt's HTML importer: don't reenter that importer
        # or enqueue a separate Qt event for every URL rejected under a burst.
        self._failure_notifications[key] = None
        while len(self._failure_notifications) > MAX_FAILED:
            self._failure_notifications.popitem(last=False)
        if not self._failure_timer.isActive():
            self._failure_timer.start()

    def _emit_image_failures(self) -> None:
        if self._closed:
            return
        for _n in range(min(FAILURE_NOTIFY_BATCH, len(self._failure_notifications))):
            key, _value = self._failure_notifications.popitem(last=False)
            self.image_failed.emit(key)
        if self._failure_notifications:
            self._failure_timer.start()

    def _image_data(self, key: str, data: QByteArray) -> None:
        if self._closed:
            return
        self.pending.discard(key)
        img = self._decode(key, data) if 0 < data.size() <= MAX_IMAGE_BYTES else None
        if img is None:
            self._failed_image(key)
            return
        restored = self.failed.pop(key, None) is not None
        self._attempts.pop(key, None)
        self._store(key, img)
        self._loaded_keys.add(key)
        self._relayout.start()
        if not self._anim.isActive():
            self._anim.start()
        if restored:
            self.image_restored.emit(key)  # its lines show the name so far

    def _schedule_image_retry(self) -> None:
        if self._closed:
            return
        due = min(self.failed.values(), default=RETRYING)
        if due == RETRYING:
            self._image_retry.stop()  # no failed image is waiting for another attempt
            return
        delay = due - time.monotonic()
        # Recovery must not add a new request queue while earlier downloads are slow.
        minimum = 100 if len(self.pending) >= RETRY_BATCH else 1
        delay_ms = max(minimum, round(delay * 1000))
        remaining = self._image_retry.remainingTime()
        if remaining < 0 or delay_ms < remaining:
            # A newly failed image may be due before an older image's long backoff.
            # Later deadlines must not keep postponing an already scheduled retry.
            self._image_retry.start(delay_ms)

    def _retry_images(self) -> None:
        """Tries a bounded batch of failed images again – in the background. The chat is
        not touched: an image stays "failed" (its lines show the name) until an attempt
        succeeds, and only then are its lines rendered again (image_restored).

        Re-rendering the lines for every attempt instead made an idle chat flicker: an
        image that can't be loaded at all (blocked, gone) switched its lines to the empty
        image and back to the name every 30 seconds – a platform icon did that to every
        line of the chat."""
        if self._closed:
            return
        now = time.monotonic()
        capacity = max(0, RETRY_BATCH - len(self.pending))
        expired = [key for key, deadline in self.failed.items() if deadline <= now][:capacity]
        for key in expired:
            self.failed[key] = RETRYING
            self.pending.add(key)
            if not self._images.request(key):
                self._failed_image(key)
        self._schedule_image_retry()

    def _decode(self, key: str, data: QByteArray) -> QImage | None:
        if self._closed:
            return None
        checked = raster_reader(data)
        if checked is None:
            return None
        reader, buf = checked
        first_size = reader.size()
        count = reader.imageCount() if reader.supportsAnimation() else 1
        target = self._target_size(first_size)
        working = (first_size.width() * first_size.height() * 3 + target.width() * target.height() * 2) * 4
        animated = (1 < count <= MAX_ANIMATION_FRAMES
                    and first_size.width() * first_size.height() * count <= MAX_ANIMATION_PIXELS
                    and target.width() * target.height() * count * 4 <= MAX_ANIMATION_FRAME_BYTES
                    and len(self.movies) < MAX_MOVIES
                    and self._movie_bytes + data.size() <= MAX_MOVIE_BYTES
                    and self._movie_working_bytes + working <= MAX_MOVIE_WORKING_BYTES)
        if animated:
            buf.seek(0)
            movie = QMovie(buf, QByteArray(), self)
            buf.setParent(movie)
            movie.setScaledSize(target)
            frames_bytes = target.width() * target.height() * count * 4
            cached = (frames_bytes <= MAX_FRAME_CACHE_PER_MOVIE
                      and self._frame_cache_bytes - self._frame_cache.get(key, 0) + frames_bytes <= MAX_FRAME_CACHE_BYTES)
            movie.setCacheMode(QMovie.CacheMode.CacheAll if cached else QMovie.CacheMode.CacheNone)
            if movie.isValid() and movie.jumpToFrame(0):
                self._drop_movie(key)
                movie.setProperty("multichat_movie_key", key)
                frame_connection = movie.frameChanged.connect(self._movie_frame_changed)
                self.movies[key] = (movie, buf)
                self._movie_bytes += buf.size()
                self._movie_costs[key] = working
                self._movie_working_bytes += working
                if cached:
                    self._frame_cache[key] = frames_bytes
                    self._frame_cache_bytes += frames_bytes
                resume = self._movie_resume[key] = QTimer(movie, singleShot=True)
                resume.setProperty("multichat_movie_key", key)
                self._movie_connections[key] = [frame_connection, resume.timeout.connect(self._movie_timeout)]
                # Frame zero is enough until visibility selection starts at most MAX_ANIMATED.
                frame = movie.currentImage()
                frame.setDevicePixelRatio(self.devicePixelRatioF())
                return frame
            movie.deleteLater()
            return None
        image = read_scaled(reader, target)
        return self._scaled(image) if image is not None else None

    def _drop_movie(self, key: str) -> None:
        entry = self.movies.pop(key, None)
        if entry:
            movie, buffer = entry
            self._movie_bytes -= buffer.size()
            self._movie_working_bytes -= self._movie_costs.pop(key)
            self._frame_cache_bytes -= self._frame_cache.pop(key, 0)
            self._playing.discard(key)
            for connection in self._movie_connections.pop(key):
                QObject.disconnect(connection)
            self._movie_resume.pop(key).stop()
            movie.stop()
            movie.deleteLater()

    @pyqtSlot(int)
    def _movie_frame_changed(self, _number: int) -> None:
        if not self._closed and (sender := self.sender()) is not None:
            self._next_frame(sender.property("multichat_movie_key"))

    @pyqtSlot()
    def _movie_timeout(self) -> None:
        if not self._closed and (sender := self.sender()) is not None:
            self._resume_movie(sender.property("multichat_movie_key"))

    def _next_frame(self, key: str) -> None:
        if self._closed:
            return
        entry = self.movies.get(key)
        if not entry:
            return
        frame = entry[0].currentImage()
        frame.setDevicePixelRatio(self.devicePixelRatioF())
        self._store(key, frame)
        if key in self.movies:
            movie = entry[0]
            delay = movie.nextFrameDelay()
            if movie.state() == QMovie.MovieState.Running and delay < FRAME_MS:
                movie.setPaused(True)
                self._movie_resume[key].start(max(1, FRAME_MS - max(0, delay)))
        if not self._repaint.isActive():
            self._repaint.start()  # same size → a repaint is enough, no re-layout

    def _resume_movie(self, key: str) -> None:
        if self._closed:
            return
        if key in self._playing and key in self.movies:
            self.movies[key][0].setPaused(False)

    def _store(self, key: str, img: QImage) -> None:
        if self._closed:
            return
        self.cache[key] = img
        self.document().addResource(IMAGE_RES, QUrl(key), img)
        while len(self.cache) > MAX_CACHED or self.cache.byte_size > MAX_CACHE_BYTES:
            old, _img = self.cache.popitem(last=False)
            # the document keeps its own reference – drop it as well, or the limit only applies
            # to this dict (an invalid value makes the document ask loadResource() again)
            self.document().addResource(IMAGE_RES, QUrl(old), None)
            self._drop_movie(old)

    def touch(self, key: str) -> None:
        """Marks an image as just shown (least recently shown ones leave the cache first)."""
        if not self._closed and key in self.cache:
            self.cache.move_to_end(key)
            if key in self.movies and not self._anim.isActive():
                self._anim.start()

    # ---- What is on screen ------------------------------------------------ #
    @staticmethod
    def _block_images(block) -> set[str]:
        names = set()
        it = block.begin()
        while not it.atEnd():
            fmt = it.fragment().charFormat()
            if fmt.isImageFormat():
                names.add(fmt.toImageFormat().name())
            it += 1
        return names

    def _visible_images(self) -> set[str]:
        vp = self.viewport()
        block = self.cursorForPosition(vp.rect().topLeft()).block()
        bottom = self.cursorForPosition(vp.rect().bottomRight()).block().blockNumber()
        names: set[str] = set()
        while block.isValid() and block.blockNumber() <= bottom:
            names |= self._block_images(block)
            block = block.next()
        return names

    def update_animations(self) -> None:
        """Only emotes on screen are animated – off-screen ones, or all of them while the window
        is minimised or hidden, are paused (each running animation repaints the chat)."""
        if self._closed or not self.movies:
            return
        win = self.window()
        active = self.isVisible() and not win.isMinimized()
        visible = self._visible_images() if active else set()
        self._playing = set([key for key in reversed(self.cache) if key in visible and key in self.movies][:MAX_ANIMATED])
        # Release old slots before starting newly visible movies, even within this GUI turn.
        for key, (movie, _buf) in self.movies.items():
            if key not in self._playing:
                self._movie_resume[key].stop()
                if movie.state() == QMovie.MovieState.Running:
                    movie.setPaused(True)
        for key in self._playing:
            movie = self.movies[key][0]
            state = movie.state()
            if state != QMovie.MovieState.Running:
                if state == QMovie.MovieState.NotRunning:
                    movie.start()
                elif not self._movie_resume[key].isActive():
                    movie.setPaused(False)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._closed:
            self._anim.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.update_animations()

    def _do_relayout(self) -> None:
        """Newly loaded images change line heights – lay out just the lines that show them
        (relaying out the whole chat took ~40 ms each time a batch of emotes arrived)."""
        if self._closed:
            return
        keys, self._loaded_keys = self._loaded_keys, set()
        doc = self.document()
        block = doc.lastBlock()
        for _n in range(RELAYOUT_SCAN):  # images are requested by recent lines
            if not block.isValid():
                break
            if self._block_images(block) & keys:
                doc.markContentsDirty(block.position(), block.length())
            block = block.previous()

    @staticmethod
    def _block_format(bg: str | None) -> QTextBlockFormat:
        fmt = QTextBlockFormat()
        if bg:  # highlighted alert line
            fmt.setBackground(QColor(bg))
            fmt.setTopMargin(3)
            fmt.setBottomMargin(3)
            fmt.setLeftMargin(4)
        return fmt

    def append_many(self, items: list) -> None:
        """Appends several messages in one edit block – one layout pass instead of one per message
        (about twice as fast as append() in a full chat). Items come from items(): (uid, html,
        background or None); a plain HTML string also works. The uid is kept on the text block,
        so update_messages() can later change single lines without rebuilding everything."""
        if not items:
            return
        doc = self.document()
        cur = QTextCursor(doc)
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.beginEditBlock()
        self.inserted += len(items)
        for item in items:
            uid, h, bg = item if isinstance(item, tuple) else (-1, item, None)
            if not doc.isEmpty():
                cur.insertBlock(QTextBlockFormat(), QTextCharFormat())  # no formatting carried over
            cur.insertHtml(h)
            cur.setBlockFormat(self._block_format(bg))
            cur.block().setUserState(uid)
        cur.endEditBlock()

    @property
    def needs_compaction(self) -> bool:
        return self.inserted > COMPACT_AFTER

    def update_messages(self, items: list) -> int:
        """Re-renders only the lines of these messages (deleted, selection changed …) instead of
        the whole chat – a full rebuild of 1000 lines blocks the window for a noticeable moment.
        Returns how many lines were updated."""
        wanted = {uid: (h, bg) for uid, h, bg in items}
        if not wanted:
            return 0
        doc = self.document()
        cur = QTextCursor(doc)
        done = 0
        self._programmatic += 1
        cur.beginEditBlock()
        try:
            block = doc.lastBlock()  # changes usually concern recent lines
            while block.isValid() and done < len(wanted):
                hit = wanted.get(block.userState())
                if hit:
                    cur.setPosition(block.position())
                    cur.setPosition(block.position() + block.length() - 1, QTextCursor.MoveMode.KeepAnchor)
                    cur.removeSelectedText()
                    cur.setCharFormat(QTextCharFormat())
                    cur.insertHtml(hit[0])
                    cur.setBlockFormat(self._block_format(hit[1]))
                    done += 1
                    self.inserted += 1
                block = block.previous()
        finally:
            cur.endEditBlock()
            self._programmatic -= 1
        if self.follow:
            self.scroll_to_bottom()
        return done

    def mentions_me(self, m: ChatMessage) -> bool:
        """The message names the account you are logged in with on its platform (your own
        messages don't count)."""
        own = self.own.get(m.platform)
        if not own or not m.text:
            return False
        pattern, user_id, name = own
        if (user_id and m.user_id == user_id) or m.user.lower() == name:
            return False
        return bool(pattern.search(m.text))

    def items(self, msgs) -> list:
        """Messages → (uid, html, background) for append_many/rebuild/update_messages."""
        out = []
        for m in msgs:
            bg = alerts.COLORS[m.event][0] if m.event and alerts.is_alert(m, self.alerts_cfg) else None
            if bg is None and self.mentions_me(m):
                bg = MENTION_BG
            out.append((m.uid, self.message_html(m), bg))
        return out

    # ---- Font size and selection ----------------------------------------- #
    def _apply_style(self) -> None:
        self.setStyleSheet(
            f"QTextBrowser {{ background:#18181b; color:#efeff1; font-size:{self.font_px}px; border:none; }}"
        )

    def set_font_px(self, px: int) -> None:
        """Chat text size; emotes scale with it. Cached images are rescaled by loading them again."""
        px = max(FONT_RANGE[0], min(FONT_RANGE[1], int(px)))
        if px == self.font_px:
            return
        if not self.follow and self._rebuild_anchor is None:
            self._rebuild_anchor = self._read_anchor()
        self.font_px = px
        self.emote_h = round(EMOTE_H * px / DEFAULT_FONT)
        self.placeholder = QImage(self.emote_h, self.emote_h, QImage.Format.Format_ARGB32)
        self.placeholder.fill(Qt.GlobalColor.transparent)
        for key in list(self.movies):
            self._drop_movie(key)
        self.cache.clear()
        self._programmatic += 1
        try:
            self.document().clear()  # also drops the document's image resources
        finally:
            self._programmatic -= 1
        self._apply_style()
        self.rerender_needed.emit()

    # The name links are all just "msg:" – the line itself knows its message (block user
    # state). A unique link per message ("msg:<uid>") made Qt keep one text format per
    # message in the document: memory grew with every message until a full re-render.
    def message_at(self, pos) -> QUrl | None:
        """"msg:<uid>" of the name/headline link at this viewport position, else None."""
        if self.anchorAt(pos) != "msg:":
            return None
        uid = self.cursorForPosition(pos).block().userState()
        return QUrl(f"msg:{uid}") if uid > 0 else None

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        target = self.message_at(event.position().toPoint()) if event.button() == Qt.MouseButton.LeftButton else None
        super().mouseReleaseEvent(event)  # ends a selection drag; its anchorClicked isn't used
        if target is not None:
            self.name_clicked.emit(target)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        target = self.message_at(event.pos())
        if target is not None:
            self.name_context.emit(target)
            return
        super().contextMenuEvent(event)

    # ---- Rendering ------------------------------------------------------- #
    def _part_html(self, part) -> str:
        kind, value, alt = part
        if kind == "emote" and value not in self.failed:
            self.touch(value)
            return f"<img src='{html.escape(value, quote=True)}' height='{self.emote_h}' align='middle'>"
        return html.escape(alt if kind == "emote" else value)

    def icon_html(self, name: str) -> str:
        h = round(self.font_px * 1.1)
        return f"<img src='mcicon:{name}' width='{h}' height='{h}' align='middle'>"

    def message_html(self, m: ChatMessage) -> str:
        color, tag = BADGES[m.platform]
        name_color = html.escape(m.color or "#b9a3e3", quote=True)
        ts = time.strftime("%H:%M", time.localtime(m.timestamp))
        body = "".join(self._part_html(p) for p in m.parts)
        if m.deleted:
            body = f"<s style='color:#666'>{body}</s> <i style='color:#666'>({html.escape(_('deleted'))})</i>"
        icon = next((u for u in FAVICONS.get(m.platform, []) if u not in self.failed), None)
        badge = (
            f"<img src='{html.escape(icon, quote=True)}' width='16' height='16' align='middle'>"
            if icon
            else f"<span style='background:{color};color:#000;font-size:11px'>&nbsp;{tag}&nbsp;</span>"
        )
        roles = "".join(self.icon_html(r) for r in ROLE_ORDER if r in m.badges)
        roles = f"{roles}&nbsp;" if roles else ""
        selected = self.selected == (m.platform, user_key(m))
        name_style = f"color:{name_color};font-weight:bold;text-decoration:none"
        if selected:
            name_style += ";background:#3a3a55"
        name = f"<a href='msg:' style='{name_style}'>{html.escape(m.user)}</a>"  # see message_at()
        head = f"<span style='color:#888'>{ts}</span> {badge} "
        if m.event:
            title = html.escape(alerts.headline(m))
            if alerts.is_alert(m, self.alerts_cfg):
                title = (f"<span style='color:{alerts.COLORS[m.event][1]};font-weight:bold'>"
                         f"{self.icon_html(m.event)} {title}</span>")
            else:
                title = f"<span style='color:#aaa'>{title}</span>"
            # the name stays clickable (user card, moderation) through the link below or the headline
            if not m.parts:
                return f"{head}<a href='msg:' style='text-decoration:none'>{title}</a>"
            return f"{head}{title}<br>{roles}{name}: {body}"
        return f"{head}{roles}{name}: {body}"

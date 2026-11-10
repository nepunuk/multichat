# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded Qt image transfers and raster metadata checks before decoding."""
from __future__ import annotations

import logging
import os
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QSize, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QImage, QImageReader
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

log = logging.getLogger("multichat.images")

# Images are loaded over https only. Plain http to this machine is for the tests' local
# servers and off otherwise: image addresses come from the platforms and the emote
# services, and none of them should be able to make the app send requests to a service
# that listens on the user's own computer.
LOOPBACK_HTTP = bool(os.environ.get("MULTICHAT_TEST_LOOPBACK_IMAGES"))


def allowed_image_url(url: QUrl) -> bool:
    if url.scheme() == "https":
        return True
    return LOOPBACK_HTTP and url.scheme() == "http" and url.host() in ("127.0.0.1", "localhost")

READ_BUFFER_BYTES = 64 * 1024
TRANSFER_TIMEOUT_MS = 15_000
MAX_IMAGE_PIXELS = 1024 * 1024
MAX_IMAGE_SIDE = 2048
MAX_DECODER_ALLOCATION_MIB = 16
RASTER_FORMATS = {b"png", b"jpeg", b"jpg", b"gif", b"webp", b"bmp", b"ico"}


@dataclass
class _Transfer:
    reply: QNetworkReply
    timer: QTimer
    data: QByteArray = field(default_factory=QByteArray)
    rejected: bool = False
    connections: list = field(default_factory=list)


class ImageTransfers(QObject):
    """Limit before NAM.get(), with bounded per-host round-robin waiting.

    A full waiting queue defers new URLs (False); it never creates another Qt
    request/event. Callers decide whether a later render or their backoff retries.
    """

    loaded = pyqtSignal(str, object)  # URL, bounded QByteArray
    failed = pyqtSignal(str)

    def __init__(self, parent, *, max_active: int, max_waiting: int, max_bytes: int,
                 user_agent: str = "MultiChat", timeout_ms: int = TRANSFER_TIMEOUT_MS):
        super().__init__(parent)
        if min(max_active, max_bytes, timeout_ms) < 1 or max_waiting < 0:
            raise ValueError("image transfer limits must be positive")
        self.max_active, self.max_waiting, self.max_bytes = max_active, max_waiting, max_bytes
        self.user_agent, self.timeout_ms = user_agent, timeout_ms
        self.nam = QNetworkAccessManager(self)
        self.nam.setTransferTimeout(timeout_ms)
        self.nam.setRedirectPolicy(QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        self.active: dict[str, _Transfer] = {}
        self.waiting: OrderedDict[str, deque[str]] = OrderedDict()
        self._queued: set[str] = set()
        self._closed = False

    @staticmethod
    def _disconnect(state: _Transfer) -> None:
        state.timer.stop()
        for connection in state.connections:
            QObject.disconnect(connection)
        state.connections.clear()

    def shutdown(self) -> None:
        """Disconnect before aborting: abort may synchronously emit finished/error signals."""
        if self._closed:
            return
        self._closed = True
        self.cancel_all()

    def cancel_all(self) -> None:
        """Discard current requests without closing this reusable transport.

        Clear queues and disconnect every reply before aborting any of them: an
        abort must neither publish an obsolete failure nor start a queued image.
        """
        self.waiting.clear()
        self._queued.clear()
        states = list(self.active.values())
        self.active.clear()
        for state in states:
            self._disconnect(state)
            state.rejected = True
            state.data.clear()
        for state in states:
            state.reply.abort()
            state.reply.deleteLater()
            state.timer.deleteLater()

    def request(self, key: str) -> bool:
        if self._closed:
            return False
        if key in self.active or key in self._queued:
            return True
        url = QUrl(key)
        if not allowed_image_url(url):
            return False
        if len(self.active) < self.max_active and not self._queued:
            self._start(key)
            return True
        if len(self._queued) >= self.max_waiting:
            return False
        self.waiting.setdefault(url.host(), deque()).append(key)
        self._queued.add(key)
        self._pump()
        return True

    def _pump(self) -> None:
        while not self._closed and self._queued and len(self.active) < self.max_active:
            host, queue = self.waiting.popitem(last=False)
            key = queue.popleft()
            self._queued.remove(key)
            if queue:
                self.waiting[host] = queue
            self._start(key)

    def _start(self, key: str) -> None:
        if self._closed:
            return
        request = QNetworkRequest(QUrl(key))
        request.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, self.user_agent)
        request.setTransferTimeout(self.timeout_ms)
        reply = self.nam.get(request)
        reply.setReadBufferSize(READ_BUFFER_BYTES)
        timer = QTimer(self, singleShot=True, interval=self.timeout_ms)
        state = self.active[key] = _Transfer(reply, timer)
        reply.setProperty("multichat_image_key", key)
        timer.setProperty("multichat_image_key", key)
        # QObject-bound slots have a receiver lifetime; Python closures capturing
        # self/state instead form cycles with the reply and its slot proxy.
        state.connections = [timer.timeout.connect(self._timed_out),
                             reply.metaDataChanged.connect(self._headers_changed),
                             reply.readyRead.connect(self._ready_read),
                             reply.downloadProgress.connect(self._download_progress),
                             reply.finished.connect(self._finished)]
        timer.start()  # wall deadline also covers peers trickling a byte before transfer timeout
        self._headers(key, state)

    def _signal_state(self):
        sender = self.sender()
        if self._closed or sender is None:
            return None
        key = sender.property("multichat_image_key")
        state = self.active.get(key)
        if state is None or sender not in (state.reply, state.timer):
            return None
        return key, state

    @pyqtSlot()
    def _timed_out(self) -> None:
        if current := self._signal_state():
            self._abort(*current)

    @pyqtSlot()
    def _headers_changed(self) -> None:
        if current := self._signal_state():
            self._headers(*current)

    @pyqtSlot()
    def _ready_read(self) -> None:
        if current := self._signal_state():
            self._read(*current)

    @pyqtSlot("qint64", "qint64")
    def _download_progress(self, received: int, total: int) -> None:
        if current := self._signal_state():
            self._progress(*current, received, total)

    @pyqtSlot()
    def _finished(self) -> None:
        if current := self._signal_state():
            self._finish(*current)

    def _headers(self, key: str, state: _Transfer) -> None:
        if self._closed or self.active.get(key) is not state:
            return
        try:
            length = int(state.reply.header(QNetworkRequest.KnownHeaders.ContentLengthHeader) or 0)
        except (TypeError, ValueError, OverflowError):
            length = 0
        if length > self.max_bytes:
            self._abort(key, state)

    def _progress(self, key: str, state: _Transfer, received: int, total: int) -> None:
        if self._closed or self.active.get(key) is not state:
            return
        if received > self.max_bytes or total > self.max_bytes:
            self._abort(key, state)

    def _read(self, key: str, state: _Transfer) -> None:
        if self._closed or self.active.get(key) is not state or state.rejected:
            return
        remaining = self.max_bytes - state.data.size()
        available = state.reply.bytesAvailable()
        if available > remaining:
            self._abort(key, state)
            return
        if available <= 0:
            return
        # Exactly what is buffered, never more: asked for more than it has, a reply that
        # has finished answers -1 ("read past the end"; PyQt: None) – unless Qt used its
        # zero-copy buffer, which it only does for a plain HTTP/1.1 response with a
        # Content-Length. The final read in _finish treated that as an error, so every
        # image from a server that compresses, sends chunked or speaks HTTP/2 (all the
        # emote CDNs) was thrown away after it had arrived completely.
        chunk = state.reply.read(available)
        if chunk is None:
            self._abort(key, state)
            return
        state.data.append(chunk)
        if state.data.size() > self.max_bytes:
            self._abort(key, state)

    def _abort(self, key: str, state: _Transfer) -> None:
        if not self._closed and self.active.get(key) is state:
            state.rejected = True
            self._disconnect(state)
            state.reply.abort()
            self._finish(key, state)  # release the slot even if a backend delays finished

    def _finish(self, key: str, state: _Transfer) -> None:
        if self._closed or self.active.get(key) is not state:
            return
        if not state.rejected and state.reply.error() == QNetworkReply.NetworkError.NoError:
            self._read(key, state)
        if self.active.get(key) is not state:
            return  # _read aborted this transfer
        del self.active[key]
        self._disconnect(state)
        state.timer.deleteLater()
        success = not state.rejected and state.reply.error() == QNetworkReply.NetworkError.NoError
        if not success and log.isEnabledFor(logging.DEBUG):  # verbose log: why an image is missing
            reason = ("stopped (too large, too slow or unreadable)" if state.rejected
                      else state.reply.errorString() or "failed")
            log.debug("Image not loaded: %s – %s", key.split("?", 1)[0][:200], reason)
        data, state.data = state.data, QByteArray()  # reply signal closures must not retain completed bodies
        state.reply.deleteLater()
        self._pump()
        if self._closed:
            return
        if success:
            self.loaded.emit(key, data)
        else:
            self.failed.emit(key)


def _gif_geometry(data: QByteArray, width: int, height: int) -> bool:
    """A small GIF canvas must not hide a much larger later image descriptor."""
    raw = memoryview(data)
    if len(raw) < 13:
        return False
    position = 13 + (3 * (1 << ((raw[10] & 7) + 1)) if raw[10] & 128 else 0)
    blocks = frames = 0

    def subblocks(pos):
        nonlocal blocks
        while pos < len(raw) and blocks < 32768:
            length = raw[pos]
            pos += 1
            blocks += 1
            if not length:
                return pos
            pos += length
        return len(raw) + 1

    while position < len(raw) and blocks < 32768 and frames <= 512:
        marker = raw[position]
        position += 1
        blocks += 1
        if marker == 0x3B:
            return frames > 0
        if marker == 0x21:
            position = subblocks(position + 1)  # extension label, then data subblocks
        elif marker == 0x2C and position + 9 <= len(raw):
            x, y, w, h = [raw[position + i] | raw[position + i + 1] << 8 for i in (0, 2, 4, 6)]
            packed = raw[position + 8]
            if not w or not h or x + w > width or y + h > height:
                return False
            position += 9 + (3 * (1 << ((packed & 7) + 1)) if packed & 128 else 0)
            position = subblocks(position + 1)  # LZW code size, then data subblocks
            frames += 1
        else:
            return False
    return False


def raster_reader(data: QByteArray) -> tuple[QImageReader, QBuffer] | None:
    """Reject large/unknown geometry and vector/plugin formats before a pixel decode."""
    # Qt's allocation guard also catches formats whose later frame geometry differs
    # from their initial canvas. Never weaken a stricter limit selected by the host.
    allocation = QImageReader.allocationLimit()
    if not allocation or allocation > MAX_DECODER_ALLOCATION_MIB:
        QImageReader.setAllocationLimit(MAX_DECODER_ALLOCATION_MIB)
    buffer = QBuffer()
    buffer.setData(data)
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    image_format = bytes(reader.format()).lower()
    if image_format not in RASTER_FORMATS:
        return None  # don't ask a vector plugin to parse its document just to find its size
    size = reader.size()
    if (not size.isValid()
            or max(size.width(), size.height()) > MAX_IMAGE_SIDE
            or size.width() * size.height() > MAX_IMAGE_PIXELS):
        return None
    if image_format == b"gif" and not _gif_geometry(data, size.width(), size.height()):
        return None
    return reader, buffer


def read_scaled(reader: QImageReader, size: QSize) -> QImage | None:
    reader.setScaledSize(size)
    image = reader.read()
    if image.isNull() or image.width() * image.height() > MAX_IMAGE_PIXELS:
        return None
    return image


class ImageCache(OrderedDict):
    """Count actual QImage storage, including assignments of animated frames."""

    def __init__(self):
        super().__init__()
        self.byte_size = 0

    def __setitem__(self, key, value):
        old = self.get(key)
        self.byte_size += value.sizeInBytes() - (old.sizeInBytes() if old is not None else 0)
        super().__setitem__(key, value)

    def popitem(self, last=True):
        key, value = super().popitem(last)
        self.byte_size -= value.sizeInBytes()
        return key, value

    def clear(self):
        super().clear()
        self.byte_size = 0

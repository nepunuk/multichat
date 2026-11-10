# SPDX-License-Identifier: GPL-3.0-or-later
"""Clearing/reusing a group and closing with failed persistence release their work."""
import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QMessageBox

from multichat.chat.models import ChatMessage
from multichat.core import config
from multichat.ui import image_io, main_window
from multichat.ui.moderation.store import ModNotes
from test_image_limits import png


def wait_until(app, predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(5)
    assert predicate()


@pytest.fixture
def window(_session_qapplication, monkeypatch):
    monkeypatch.setattr(main_window, "load_mpv", lambda: None)
    monkeypatch.setattr(image_io, "allowed_image_url",
                        lambda url: url.scheme() == "http" and url.host() == "127.0.0.1")
    cfg = config.load_config()
    cfg.update(extra_emotes=False, check_updates=False, groups=config.clean_groups(None, {}),
               active_group=0, side_by_side=False)
    cfg["video"]["enabled"] = cfg["overlay"]["enabled"] = False
    w = main_window.MainWindow(cfg)
    w.show()
    _session_qapplication.processEvents()
    try:
        yield w
    finally:
        # Also clean up a regression run against the old, partly closed window.
        if w._closing and w._loop_thread.is_alive():
            w._closing = False
            w.show()
        w.close()


@pytest.fixture
def group_images():
    requested = []
    release = threading.Event()
    payload = png()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requested.append(self.path)
            if self.path == "/missing.png" and requested.count(self.path) == 1:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                if self.path == "/slow.png":
                    self.wfile.write(payload[:16])
                    self.wfile.flush()
                    release.wait(5)
                    self.wfile.write(payload[16:])
                else:
                    self.wfile.write(payload)
                self.wfile.flush()
            except OSError:
                pass  # Reset intentionally aborts the stalled response.

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", release, requested
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("action", ["clear", "remove"])
def test_group_reset_stops_retries_and_inflight_images_but_allows_reuse(
        window, _session_qapplication, group_images, monkeypatch, action):
    w, app = window, _session_qapplication
    base, release, requested = group_images
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    panel = w.add_group()
    panel.rows["twitch"].edit.setText("synthetic_channel")
    view, transfers = panel.view, panel.view._images
    transfers.max_active = 1
    missing, slow, queued = (base + path for path in ("/missing.png", "/slow.png", "/queued.png"))

    def show_images(*urls):
        message = ChatMessage("twitch", "synthetic_channel", "SyntheticUser", "emotes",
                              parts=[("emote", url, "SyntheticEmote") for url in urls],
                              source="twitch:synthetic_channel")
        w.add_message(message)
        w._flush_messages()
        w._render_now()

    show_images(missing)
    wait_until(app, lambda: missing in view.failed)
    assert requested == ["/missing.png"] and view._image_retry.isActive()
    show_images(slow, queued)
    wait_until(app, lambda: slow in transfers.active and transfers.active[slow].data.size() == 16)
    assert transfers._queued == {queued}
    state = transfers.active[slow]
    delivered = [QSignalSpy(transfers.loaded), QSignalSpy(transfers.failed), QSignalSpy(view.image_failed)]
    # Make the old failure due, then clear before its timer gets another GUI turn.
    view.failed[missing] = time.monotonic() - 1

    if action == "remove":
        w.remove_group(panel)
        assert panel in w._spare and not panel.isVisible()
    else:
        w.clear_chat()

    assert view.toPlainText() == "" and not view.retained_messages
    assert not transfers.active and not transfers._queued and not transfers.waiting
    assert not view.pending and not view.failed and not view._failure_notifications
    assert not view._image_retry.isActive() and not view._failure_timer.isActive()
    assert not state.connections and state.data.isEmpty()
    view._retry_images()  # Even an already due timeout has nothing obsolete to request.
    release.set()
    for _ in range(5):
        app.processEvents()
        QTest.qWait(5)
    assert requested == ["/missing.png", "/slow.png"]
    assert all(len(spy) == 0 for spy in delivered)
    assert slow not in view.cache and not view.pending and not view.failed

    if action == "remove":
        assert w.add_group() is panel  # Same widget and transport can be reused.
        panel.rows["twitch"].edit.setText("synthetic_channel")
    show_images(missing)
    wait_until(app, lambda: missing in view.cache)
    assert requested == ["/missing.png", "/slow.png", "/missing.png"]
    assert not view.failed and not view._closed and not transfers._closed


def test_close_flushes_notes_and_shuts_down_after_real_config_write_failure(
        window, _session_qapplication, monkeypatch, tmp_path, caplog):
    w = window
    started, stopped = threading.Event(), threading.Event()

    async def reader():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            stopped.set()

    w.readers["twitch:synthetic_channel"] = asyncio.run_coroutine_threadsafe(reader(), w.loop)
    assert started.wait(2)
    w.mod.notes = ModNotes(tmp_path / "notes.json")
    message = ChatMessage("twitch", "synthetic_channel", "SyntheticUser", "message", user_id="synthetic")
    w.mod.notes.set_note(message, "Flush this pending note during close")
    assert w.mod.notes._dirty and not w.mod.notes.path.exists()
    blocked = tmp_path / "cannot-replace-directory.json"
    blocked.mkdir()
    uncaught = []
    with monkeypatch.context() as patch:
        patch.setattr(config, "CONFIG", blocked)  # The real os.replace fails, not a mocked saver.
        patch.setattr(sys, "excepthook", lambda *args: uncaught.append(args))
        w.close()  # Dispatch the actual QWidget close event.

    assert not uncaught
    assert not w.isVisible() and w._closing
    assert stopped.wait(1) and w.session.closed and not w._loop_thread.is_alive() and w.loop.is_closed()
    assert all(panel.view._closed for panel in [*w.panels, *w._spare])
    assert not w.mod.notes._dirty and not w.mod.notes._timer.isActive()
    saved = json.loads(w.mod.notes.path.read_text(encoding="utf-8"))
    assert saved[ModNotes.key(message)]["note"] == "Flush this pending note during close"
    assert "settings" in caplog.text.lower() and "closing" in caplog.text.lower()

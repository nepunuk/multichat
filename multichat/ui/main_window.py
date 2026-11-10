# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Corrgan
"""Main window: channel groups (chat + videos each), menus, send line."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import threading
from collections import OrderedDict
from concurrent.futures import Future
from pathlib import Path

import aiohttp

from PyQt6.QtCore import QEvent, QObject, QProcess, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QDesktopServices, QImageReader, QKeySequence
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QSplitter, QStyle, QTabBar, QToolButton, QVBoxLayout, QWidget,
)

from ..accounts import KickAccount, SecretStore, TwitchAccount, YouTubeAccount
from ..accounts.store import choose_backend
from ..chat import ChatMessage, EmoteStore
from ..core import i18n
from ..core.config import CONF_DIR, OVERLAY_PORT, clean_groups, load_config, load_config_apps, save_config
from .. import __version__ as APP_VERSION
from ..core.constants import LOGIN_DOCS, NAMES, PLATFORMS, REPO_SLUG, REPO_URL, version_tuple
from ..core.i18n import _
from ..core.single_instance import release_active
from ..core.logsetup import LOG_DIR, LOG_FILE, asyncio_exception_handler, set_debug, tail
from ..overlay.server import OverlayServer
from ..video.player import load_mpv
from ..video.tiles import AudioHub
from .chat_view import DEFAULT_FONT, own_pattern
from .emote_complete import EmoteCompleter
from .groups import GroupPanel
from .incoming import IncomingEvents
from .dialogs import AboutDialog, AlertsDialog, AppsDialog
from .moderation import ModActions, ModNotes
from .live_watch import LiveWatch
from .watchdog import Watchdog
from .window_connections import ConnectionsMixin
from .window_groups import MAX_MESSAGES, GroupsMixin
from .window_messages import FLUSH_MS, MessagesMixin
from .window_send import SendMixin
from ..core.config import MAX_GROUPS

log = logging.getLogger("multichat")


MAX_INPUT = 500  # longest message the platforms accept


class Bridge(QObject):
    """Safely moves events from the asyncio thread into the Qt thread."""

    message = pyqtSignal(object)
    deletion = pyqtSignal(object)
    incoming = pyqtSignal(bool)  # coalesced mailbox wakeup; True makes moderation urgent
    status = pyqtSignal(str, str, int)  # source key ("twitch:channel"), text, connection generation
    result = pyqtSignal(object, object)
    login_url = pyqtSignal(str)  # login link to show when no browser could be opened
    update = pyqtSignal(str, str)  # newer version, release page


class MainWindow(GroupsMixin, ConnectionsMixin, MessagesMixin, SendMixin, QMainWindow):
    """The window itself: construction, menus, settings, accounts, overlay, shutdown. The
    rest lives in the mixins: groups (window_groups), chat connections (window_connections),
    the message pipeline (window_messages) and the send line (window_send)."""

    def __init__(self, config: dict | None = None) -> None:
        super().__init__()
        self.setWindowTitle("MultiChat")
        self.resize(520, 780)
        self.config = config or load_config()
        if not self.config.get("groups"):
            self.config["groups"] = clean_groups(None, self.config.get("channels") or {})

        self.bridge = Bridge()
        self.bridge.message.connect(self.add_message)
        self.bridge.deletion.connect(self.apply_deletion)
        self.bridge.incoming.connect(self._schedule_flush)
        self.bridge.status.connect(self._reader_status)
        self.bridge.result.connect(self._on_result)

        self.loop = asyncio.new_event_loop()
        self.loop.set_exception_handler(asyncio_exception_handler)
        self._loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._loop_thread.start()
        self._closing = False
        self.session = asyncio.run_coroutine_threadsafe(self._make_session(), self.loop).result()

        backend, files = choose_backend(CONF_DIR)
        store = SecretStore("tokens", backend, files)
        self.apps = SecretStore("apps", backend, files)  # client IDs and secrets of the developer apps
        if self.config.pop("apps", None):  # older versions kept them in config.json
            legacy = load_config_apps()
            if legacy and not self.apps.all():
                self.apps.replace_all(legacy)
            save_config(self.config)
        log.info("Secrets: stored in %s", backend.name)
        args = (store, self.app_creds, lambda: self.session)
        self.accounts = {"twitch": TwitchAccount(*args), "kick": KickAccount(*args), "youtube": YouTubeAccount(*args)}
        self.overlay = OverlayServer(self.loop)
        qt_formats = {bytes(f).decode().lower() for f in QImageReader.supportedImageFormats()}
        # gif before png: 7TV only offers GIF (not PNG) for animated emotes when WebP is unavailable
        self.emotes = EmoteStore(lambda: self.session, [f for f in ("webp", "gif", "png") if f in qt_formats])
        self.readers: dict = {}  # source key ("twitch:channel") → running chat reader (concurrent future)
        self.conn_gen: dict[str, int] = {}  # increases with every (dis)connect of a channel
        self.panels: list[GroupPanel] = []
        self._spare: list[GroupPanel] = []  # removed groups, hidden – reused by add_group()
        self._route: dict[str, list[GroupPanel]] = {}  # source key → groups that show it
        self.messages: OrderedDict[int, ChatMessage] = OrderedDict()
        self._trim_at = MAX_MESSAGES  # message count that triggers the next _trim_messages()
        self._incoming = IncomingEvents(MAX_MESSAGES, MAX_MESSAGES * MAX_GROUPS, MAX_GROUPS * len(PLATFORMS))
        self._reported_drops = 0
        self._loss_report_at = 0.0
        self._flush_timer = QTimer(self, singleShot=True, interval=FLUSH_MS)
        self._flush_timer.timeout.connect(self._flush_messages)
        self._render_timer = QTimer(self, singleShot=True, interval=150)  # coalesces full re-renders
        self._render_timer.timeout.connect(self._render_now)
        self._rerender: set | None = set()  # groups waiting for a full re-render; None = all of them
        self._render_pending: set = set()
        self._render_step_timer = QTimer(self, singleShot=True, interval=1)
        self._render_step_timer.timeout.connect(self._render_step)

        self.mod = ModActions(self, ModNotes(CONF_DIR / "modnotes.json"))
        self._build_ui()
        self._build_menus()
        self.apply_config()
        self.refresh_accounts()
        self._update_conn_buttons()
        self.bridge.login_url.connect(self._show_login_url)
        self.bridge.update.connect(self._show_update)
        for acc in self.accounts.values():
            acc.show_url = self.bridge.login_url.emit
        if self.config.get("_warnings"):
            self.statusBar().showMessage(
                _("The settings file was damaged – started with defaults (the old file was kept as a backup)"), 15000
            )
        self._msg_count = 0
        self.watchdog = Watchdog(self, self._perf_stats)
        self.live_watch = LiveWatch(self)  # which entered channels are live – dots on rows and tabs
        self.live_watch.changed.connect(self._live_changed)
        self.live_watch.start()
        if self.config.get("check_updates", True) and not os.environ.get("MULTICHAT_NO_UPDATE_CHECK"):
            self.run(self._check_update())

    # ---- Configuration --------------------------------------------------- #
    def load_config(self) -> dict:
        return load_config()

    def save_config(self) -> None:
        self.config["groups"] = [panel.to_config() for panel in self.panels]
        self.config["active_group"] = self.panels.index(self.active_panel())
        self.config["side_by_side"] = self.side_action.isChecked()
        first = self.panels[0]  # older versions read this – keeps a downgrade working
        self.config["channels"] = {p: first.rows[p].edit.text().strip() for p in PLATFORMS}
        self.config["show"] = {p: c.isChecked() for p, c in self.filters.items()}
        self.config["on_top"] = self.top_action.isChecked()
        self.config["extra_emotes"] = self.emote_action.isChecked()
        self.config["debug_log"] = self.debug_action.isChecked()
        if self.video_ok:
            for panel in self.panels:
                self.config["video"].update(panel.video.state())
            self.config["video"]["audio"] = self.audio.audio
        self.config["video"]["enabled"] = self.video_action.isChecked()
        save_config(self.config)

    def apply_config(self) -> None:
        for p in PLATFORMS:
            self.filters[p].setChecked(self.config["show"].get(p, True))
        self.top_action.setChecked(self.config["on_top"])
        self.emote_action.setChecked(self.config["extra_emotes"])  # "on" loads the global sets
        self.emotes.enabled = self.config["extra_emotes"]
        self.debug_action.setChecked(self.config["debug_log"])
        for v in self.views():
            v.set_font_px(self.config["chat_font"])
            v.alerts_cfg = self.config["alerts"]
        video_on = bool(self.config["video"].get("enabled", True))
        self.video_action.setChecked(video_on)
        self.toggle_video(video_on, resize=False)
        self.resize(1300 if video_on else 520, 820 if video_on else 780)
        self.tabs.setCurrentIndex(min(self.config.get("active_group", 0), len(self.panels) - 1))
        self.side_action.setChecked(bool(self.config.get("side_by_side")) and len(self.panels) > 1)
        self._show_panels()
        if self.config["overlay"].get("enabled"):
            self.overlay_action.setChecked(True)

    def app_creds(self, platform: str) -> tuple[str, str]:
        app = self.apps.get(platform)
        return app.get("client_id", ""), app.get("client_secret", "")

    async def _make_session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=20))

    # ---- Layout ---------------------------------------------------------- #
    def _build_ui(self) -> None:
        self.icon_play = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        self.icon_stop = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop)

        filters = QHBoxLayout()
        filters.addWidget(QLabel(_("Show:")))
        self.filters = {}
        for p in PLATFORMS:
            cb = QCheckBox(NAMES[p])
            cb.setChecked(True)
            cb.toggled.connect(lambda _on: self.render_all())
            self.filters[p] = cb
            filters.addWidget(cb)
        filters.addStretch(1)

        # groups: all panels sit in one splitter for good – as tabs only the active one is
        # shown, side by side all of them (moving them between widgets would move their
        # OpenGL video tiles to another parent, which makes Qt recreate the GL contexts)
        self.video_ok = bool(load_mpv())
        self.audio = AudioHub(self.config["video"].get("audio"))
        self.audio.changed.connect(self._schedule_save)
        self.side_by_side = False
        self.tabs = QTabBar()
        self.tabs.setDocumentMode(True)
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        self.tabs.currentChanged.connect(lambda _i: self._tab_changed())
        add = QToolButton()
        add.setText("+")
        add.setAutoRaise(True)
        add.setToolTip(_("New channel group"))
        add.clicked.connect(lambda: self.add_group())
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(0, 0, 0, 0)
        tab_row.addWidget(add)  # left of the tabs – new groups appear on the right
        tab_row.addWidget(self.tabs)
        tab_row.addStretch(1)
        self.side = QSplitter(Qt.Orientation.Horizontal)
        self.side.setChildrenCollapsible(False)
        self._side_active: GroupPanel | None = None  # side by side: the group clicked last
        QApplication.instance().focusChanged.connect(self._focus_changed)
        for g in self.config["groups"]:
            self._add_panel(g)

        send = QHBoxLayout()
        self.target = QComboBox()
        self.target.setMinimumContentsLength(10)
        self.input = QLineEdit()
        self.input.setMaxLength(MAX_INPUT)
        self.input.returnPressed.connect(self.send)
        # ":kap" → Kappa, "@na" → @name
        self.emote_complete = EmoteCompleter(self.input, self._send_emotes, self._send_mentions)
        self._native_emotes: dict[str, dict[str, str]] = {}  # source key → Twitch emotes you can use there
        self._native_loading: set[str] = set()
        self._native_session = self.accounts["twitch"].session_gen
        self._native_hint = False
        self.send_btn = QPushButton(_("Send"))
        self.send_btn.clicked.connect(self.send)
        send.addWidget(self.target)
        send.addWidget(self.input, 1)
        send.addWidget(self.send_btn)

        if self.video_ok:
            # An OpenGL widget present from the start: adding the first one to a window that
            # is already shown makes Qt rebuild the window's rendering – with mpv contexts
            # around, that crashed (seen with the AppImage when a second group was added).
            self._keeper = QOpenGLWidget()
            self._keeper.setFixedSize(1, 1)
            filters.addWidget(self._keeper)

        root = QVBoxLayout()
        root.setContentsMargins(9, 9, 9, 0)
        root.addLayout(filters)
        root.addLayout(tab_row)
        root.addWidget(self.side, 1)
        root.addLayout(send)
        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)
        self.statusBar().showMessage(_("Tip: click a name to select the user, right-click it for the menu."), 8000)

    # ---- Groups ------------------------------------------------------------ #
    def _build_menus(self) -> None:
        mb = self.menuBar()
        view = mb.addMenu(_("View"))
        view.addAction(_("Clear chat")).triggered.connect(self.clear_chat)
        view.addAction(_("New channel group")).triggered.connect(lambda: self.add_group())
        self.side_action = view.addAction(_("Groups side by side"))
        self.side_action.setCheckable(True)
        self.side_action.setEnabled(len(self.config["groups"]) > 1)
        self.side_action.toggled.connect(self.toggle_side_by_side)
        view.addAction(_("Connect all groups")).triggered.connect(self.connect_all)
        view.addAction(_("Disconnect everything")).triggered.connect(self.disconnect_all)
        view.addSeparator()
        self.top_action = view.addAction(_("Always on top"))
        self.top_action.setCheckable(True)
        self.top_action.toggled.connect(self.toggle_on_top)
        self.emote_action = view.addAction(_("7TV / BTTV / FFZ emotes"))
        self.emote_action.setCheckable(True)
        self.emote_action.toggled.connect(self.toggle_emotes)
        self.video_action = view.addAction(_("Video tiles"))
        self.video_action.setCheckable(True)
        self.video_action.toggled.connect(self.toggle_video)
        view.addAction(_("Alerts …")).triggered.connect(self.edit_alerts)
        font = view.addMenu(_("Chat font size"))
        for label, keys, step in ((_("Larger"), ("Ctrl++", "Ctrl+="), 2), (_("Smaller"), ("Ctrl+-",), -2),
                                  (_("Default size"), ("Ctrl+0",), 0)):
            a = font.addAction(label)
            a.setShortcuts([QKeySequence(k) for k in keys])
            a.triggered.connect(lambda _checked=False, s=step: self.change_font(s))

        view.addSeparator()
        lang_menu = view.addMenu(_("Language"))
        group = QActionGroup(self)
        group.setExclusive(True)
        chosen = self.config.get("language", "auto")
        for code, label in [("auto", _("Automatic (system)")), *i18n.LANGUAGES]:
            a = lang_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(code == chosen)
            a.triggered.connect(lambda _checked=False, c=code: self.change_language(c))
            group.addAction(a)
            if code == "auto":
                lang_menu.addSeparator()

        self.accounts_menu = mb.addMenu(_("Accounts"))
        self.accounts_menu.aboutToShow.connect(self._fill_accounts_menu)
        self._fill_accounts_menu()

        ov = mb.addMenu(_("Overlay"))
        self.overlay_action = ov.addAction(_("OBS overlay active"))
        self.overlay_action.setCheckable(True)
        self.overlay_action.toggled.connect(self.toggle_overlay)
        ov.addAction(_("Copy overlay URL")).triggered.connect(self.copy_overlay_url)
        ov.addAction(_("Copy overlay URL for the active group")).triggered.connect(
            lambda: self.copy_overlay_url(self.panels.index(self.active_panel()) + 1))

        hlp = mb.addMenu(_("Help"))
        hlp.addAction(_("Copy log (last 300 lines)")).triggered.connect(self.copy_log)
        hlp.addAction(_("Open log file")).triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_FILE))))
        hlp.addAction(_("Open log folder")).triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_DIR))))
        self.debug_action = hlp.addAction(_("Verbose log (debug)"))
        self.debug_action.setCheckable(True)
        self.debug_action.toggled.connect(self.toggle_debug)
        self.update_action = hlp.addAction(_("Check for new versions at start"))
        self.update_action.setCheckable(True)
        self.update_action.setChecked(bool(self.config.get("check_updates", True)))
        self.update_action.toggled.connect(self._toggle_update_check)
        hlp.addSeparator()
        hlp.addAction(_("Login setup guide")).triggered.connect(lambda: QDesktopServices.openUrl(QUrl(LOGIN_DOCS)))
        hlp.addAction(_("About MultiChat")).triggered.connect(lambda: AboutDialog(self).exec())

    def _fill_accounts_menu(self) -> None:
        m = self.accounts_menu
        m.clear()
        for p in PLATFORMS:
            acc = self.accounts[p]
            if acc.logged_in:
                a = m.addAction(_("{platform}: {user} – log out").format(platform=NAMES[p], user=acc.user_name))
                a.triggered.connect(lambda _checked=False, p=p: self.logout(p))
            else:
                a = m.addAction(_("Log in to {platform} …").format(platform=NAMES[p]))
                a.triggered.connect(lambda _checked=False, p=p: self.login(p))
        m.addSeparator()
        m.addAction(_("Set up developer apps …")).triggered.connect(self.edit_apps)

    # ---- Helpers --------------------------------------------------------- #
    def run(self, coro, then=None, label: str | None = None, on_error=None, quiet: bool = False):
        """Run a coroutine in the asyncio thread; the result arrives in the Qt thread.
        quiet: a failure is only logged (background work the user didn't start)."""
        if self._closing:
            coro.close()
            future = Future()
            future.cancel()
            return future
        name = getattr(coro, "__qualname__", "action")  # English name for the log (the label is translated)
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        fut.add_done_callback(lambda f: self._deliver(then, label, on_error, name, f, quiet))
        return fut

    def _deliver(self, then, label, on_error, name, fut, quiet=False) -> None:
        try:
            self.bridge.result.emit((then, label, on_error, name, quiet), fut)
        except RuntimeError:  # window already closed – nobody left to tell
            pass

    def _on_result(self, meta, fut) -> None:
        if self._closing:
            return
        then, label, on_error, name, quiet = meta
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc and quiet:
            log.warning("%s failed: %s", name, exc)
            if on_error:
                on_error()
            return
        if exc:
            log.error("%s failed: %s", name, exc, exc_info=exc)
            self.statusBar().showMessage(_("Error: {error}").format(error=exc), 12000)
            if on_error:
                on_error()
            self.refresh_accounts()  # e.g. logged out after a rejected refresh token
            return
        if label:
            self.statusBar().showMessage(f"{label} ✓", 6000)
        if then:
            then(fut.result())

    def copy_log(self) -> None:
        QApplication.clipboard().setText(tail(300))
        self.statusBar().showMessage(
            _("Log copied (tokens/secrets redacted) – file: {path}").format(path=LOG_FILE), 10000
        )

    def change_font(self, step: int) -> None:
        px = self.view.font_px + step if step else DEFAULT_FONT
        for v in self.views():
            v.set_font_px(px)
        self.config["chat_font"] = self.view.font_px
        self.statusBar().showMessage(_("Chat font size: {px} px").format(px=self.view.font_px), 3000)

    def toggle_debug(self, on: bool) -> None:
        set_debug(on)
        self.config["debug_log"] = on
        log.info("Verbose log %s", "on" if on else "off")

    def change_language(self, code: str) -> None:
        if code == self.config.get("language", "auto"):
            return
        self.config["language"] = code
        self.save_config()
        log.info("Language changed to %s", code)
        answer = QMessageBox.question(
            self,
            _("Language"),
            _("The new language takes effect after a restart. Restart MultiChat now?"),
        )
        if answer == QMessageBox.StandardButton.Yes:
            restart_app(self)

    def toggle_emotes(self, on: bool) -> None:
        self.emotes.enabled = on
        if on and not self.emotes.global_loaded:
            def loaded(counts: dict) -> None:
                info = ", ".join(f"{k.upper()} {v}" for k, v in counts.items()) or _("none reachable")
                self.statusBar().showMessage(_("Global emotes loaded: {info}").format(info=info), 6000)

            self.run(self.emotes.load_global(), then=loaded)

    def toggle_on_top(self, on: bool) -> None:
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        if visible:
            self.show()

    # ---- Statistics, window state ---------------------------------------- #
    def _perf_stats(self) -> dict:
        """For the verbose log's minute summary (see watchdog.py)."""
        n, self._msg_count = self._msg_count, 0
        movies = [m for v in self.views() for m, _b in v.movies.values()]
        stats = {
            "messages/s": round(n / 60, 1),
            "queued events": len(self._incoming),
            "dropped events": self._incoming.dropped,
            "dropped moderation events": self._incoming.dropped_deletions,
            "chat lines": sum(v.document().blockCount() for v in self.views()),
            "images not loaded": sum(len(v.failed) for v in self.views()),
            "animations playing": sum(m.state() == m.MovieState.Running for m in movies),
            "of": len(movies),
        }
        for i, panel in enumerate(self.panels):
            for key, (frames, ms) in (panel.video.frame_counts() if panel.video is not None else {}).items():
                name = key if i == 0 else f"{key} (group {i + 1})"
                stats[f"{name} fps"] = round(frames / 60, 1)
                stats[f"{name} ms/frame"] = round(ms / frames, 1)
        return stats

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            for v in self.views():
                v.update_animations()  # minimised: animations pause

    # ---- Moderation (logic in moderation/actions.py) ---------------------- #
    def on_anchor(self, url: QUrl) -> None:
        """Left click on a name: select the user (the action bar and user card follow)."""
        msg = self.mod.anchor_url(url)
        if msg:
            self.mod.select(msg)

    def on_name_context(self, url: QUrl) -> None:
        """Right click on a name: select the user and open the menu."""
        msg = self.mod.anchor_url(url)
        if msg:
            self.mod.select(msg)
            self.mod.menu(msg)

    # ---- Accounts -------------------------------------------------------- #
    def refresh_accounts(self) -> None:
        generation = self.accounts["twitch"].session_gen
        if self._native_session != generation or not self.accounts["twitch"].logged_in:
            self._native_session = generation
            self._native_emotes.clear()
            self._native_loading.clear()
        self._native_hint = False
        self._refresh_targets()
        if getattr(self, "live_watch", None) is not None:
            self.live_watch.recheck("twitch")  # the Twitch check needs the login
        own = {p: (own_pattern(a.user_name), a.user_id, a.user_name.lower())
               for p, a in self.accounts.items() if a.logged_in and a.user_name}
        if any(v.own.keys() != own.keys() or any(v.own[p][2] != own[p][2] for p in own) for v in self.views()):
            for v in self.views():
                v.own = own
            if self.messages:
                self.render_all()  # lines naming you are highlighted (or no longer after a logout)

    def login(self, platform: str) -> None:
        cid, secret = self.app_creds(platform)
        if not cid or not secret:
            QMessageBox.information(
                self,
                _("Developer app missing"),
                _("To log in to {platform} you first need the client ID and secret of your own developer app.").format(
                    platform=NAMES[platform]
                ),
            )
            self.edit_apps()
            if not all(self.app_creds(platform)):
                return
        self.statusBar().showMessage(
            _("Browser opened – please log in to {platform} …").format(platform=NAMES[platform]), 30000
        )
        self.run(
            self.accounts[platform].login(),
            then=lambda who: (self.refresh_accounts(), self.mod.account_changed(platform)),
            label=_("Logged in to {platform}").format(platform=NAMES[platform]),
        )

    def _show_login_url(self, url: str) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(_("Log in"))
        box.setText(_("No browser could be opened. Open this link in your browser to log in:"))
        box.setInformativeText(url)
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        copy = box.addButton(_("Copy link"), QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(url))
        box.open()

    # ---- Updates --------------------------------------------------------- #
    async def _check_update(self) -> None:
        """Asks GitHub once for the latest release (no data about you is sent besides the request)."""
        url = f"https://api.github.com/repos/{REPO_SLUG}/releases/latest"
        headers = {"Accept": "application/vnd.github+json", "User-Agent": f"MultiChat/{APP_VERSION}"}
        try:
            async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    log.info("Update check: HTTP %s", r.status)
                    return
                data = await r.json()
        except Exception as e:  # noqa: BLE001 – offline etc.: not worth bothering anyone
            log.info("Update check failed: %s", e)
            return
        latest = str(data.get("tag_name") or "").removeprefix("v")
        if not re.fullmatch(r"[0-9]{1,4}(\.[0-9]{1,4}){1,3}", latest):
            log.info("Update check: ignoring unexpected tag %r", latest[:40])
            return
        if version_tuple(latest) > version_tuple(APP_VERSION):
            log.info("Update check: %s available (running %s)", latest, APP_VERSION)
            # the link is built here – a response can never lead to another site
            self.bridge.update.emit(latest, f"{REPO_URL}/releases/tag/v{latest}")
        else:
            log.info("Update check: %s is current", APP_VERSION)

    def _show_update(self, version: str, url: str) -> None:
        text = _("New version {version} available – open download page").format(version=version)
        self.statusBar().showMessage(text, 30000)
        act = QAction(text, self)
        act.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        self.menuBar().addAction(act)  # visible right in the menu bar

    def _toggle_update_check(self, on: bool) -> None:
        self.config["check_updates"] = on
        self.save_config()

    def logout(self, platform: str) -> None:
        try:
            saved = self.accounts[platform].logout()
        except Exception:
            saved = False
            log.exception("%s: could not persist logout", platform)
        self.refresh_accounts()
        self.mod.account_changed(platform)
        if saved:
            self.statusBar().showMessage(_("Logged out of {platform}").format(platform=NAMES[platform]), 5000)
        else:
            self.statusBar().showMessage(_("Logged out of {platform}, but could not save the logout. "
                                          "The account may return after restarting.").format(platform=NAMES[platform]), 15000)

    def edit_alerts(self) -> None:
        dlg = AlertsDialog(self, self.config["alerts"])
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.config["alerts"] = dlg.values()
            for v in self.views():
                v.alerts_cfg = self.config["alerts"]
            self.save_config()
            self.render_all()

    def edit_apps(self) -> None:
        dlg = AppsDialog(self, self.apps.all())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.apps.replace_all(dlg.values())

    # ---- Overlay --------------------------------------------------------- #
    def overlay_url(self, group: int = 0) -> str:
        """group: only that channel group (1 = first tab), 0 = all."""
        url = f"http://127.0.0.1:{self.config['overlay'].get('port', OVERLAY_PORT)}/?fade=0&size=20"
        return url + (f"&group={group}" if group else "")

    def toggle_overlay(self, on: bool) -> None:
        self.config["overlay"]["enabled"] = on
        if on:
            port = int(self.config["overlay"].get("port", OVERLAY_PORT))
            self.run(
                self.overlay.start(port),
                then=lambda _result: self.statusBar().showMessage(
                    _("Overlay running: {url} – add it in OBS as a browser source").format(url=self.overlay_url()),
                    15000,
                ),
                on_error=lambda: self.overlay_action.setChecked(False),
            )
        else:
            self.run(self.overlay.stop())

    def copy_overlay_url(self, group: int = 0) -> None:
        url = self.overlay_url(group)
        QApplication.clipboard().setText(url)
        hint = "" if self.overlay.running else " " + _("(overlay is still off – enable it under “Overlay”)")
        self.statusBar().showMessage(_("Copied: {url}").format(url=url) + hint, 8000)

    # ---- Shutdown -------------------------------------------------------- #
    def closeEvent(self, event) -> None:  # noqa: N802
        if self._closing:
            super().closeEvent(event)
            return
        self._closing = True
        release_active()  # a start from now on opens a new window (also restart_app)
        self.live_watch.stop()
        self._flush_timer.stop()
        self._render_timer.stop()
        self._render_step_timer.stop()
        self._render_pending.clear()
        if hasattr(self, "_save_timer"):
            self._save_timer.stop()
        self.emote_complete.shutdown()
        # Persistence failures must not skip the other save or resource cleanup.
        # In particular, a settings error must not lose a pending note on exit.
        try:
            self.save_config()
        except Exception:  # noqa: BLE001
            log.exception("Saving settings while closing failed")
        try:
            self.mod.notes.save()
        except Exception:  # noqa: BLE001
            log.exception("Saving moderation notes while closing failed")
        for t in self.readers.values():
            t.cancel()
        for panel in [*self.panels, *self._spare]:
            panel.view.shutdown()
            if panel.video is not None:
                panel.video.shutdown()

        async def shutdown() -> None:
            # This event loop belongs exclusively to this window. Finish cancellation
            # before closing its HTTP session/loop, including pending account queries.
            tasks = asyncio.all_tasks() - {asyncio.current_task()}
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self.overlay.stop()
            await self.session.close()
            await self.loop.shutdown_asyncgens()

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self.loop).result(3)
        except Exception:  # noqa: BLE001
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._loop_thread.join(timeout=1)
        if not self._loop_thread.is_alive():
            self.loop.close()
        super().closeEvent(event)


def restart_app(win: QMainWindow) -> None:
    """Start a new instance (AppImage, frozen binary or source) and close this one."""
    if os.environ.get("APPIMAGE"):
        prog, args = os.environ["APPIMAGE"], []
    elif getattr(sys, "frozen", False):
        prog, args = sys.executable, []
    else:
        prog, args = sys.executable, ["-m", "multichat"]
    log.info("Restarting: %s %s", prog, " ".join(args))
    win.close()
    # from the directory that contains the package, so `-m multichat` also works from a source checkout
    QProcess.startDetached(prog, args, str(Path(__file__).resolve().parents[2]))
    QApplication.quit()

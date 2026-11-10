# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Corrgan
"""Storage for login tokens and the client secrets of the developer apps.

By default the data goes into the system keyring via the Secret Service API
(KWallet, GNOME Keyring, KeePassXC …). If none is available, it falls back to JSON
files in ~/.config/multichat/ that only the user can read (0600).
MULTICHAT_SECRETS=file forces the files, MULTICHAT_SECRETS=keyring the keyring: then
nothing is ever written to a file – without a working keyring, logins only last until
the app is closed.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from ..core.config import write_private

log = logging.getLogger("multichat.secrets")

SERVICE = "multichat"


class FileBackend:
    """One JSON file per item, readable only by the user."""

    name = "file"

    def __init__(self, directory: Path) -> None:
        self.dir = directory

    def path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def load(self, key: str) -> dict | None:
        try:
            return json.loads(self.path(key).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("%s unreadable: %s", self.path(key), e)
            return None

    def save(self, key: str, data: dict) -> None:
        write_private(self.path(key), data)

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)


class MemoryBackend:
    """MULTICHAT_SECRETS=keyring without a usable keyring: kept in memory only."""

    name = "memory (not saved – no keyring)"
    strict = True

    def __init__(self) -> None:
        self.items: dict = {}

    def load(self, key: str) -> dict | None:
        return self.items.get(key)

    def save(self, key: str, data: dict) -> None:
        self.items[key] = json.loads(json.dumps(data))

    def delete(self, key: str) -> None:
        self.items.pop(key, None)


class KeyringBackend:
    """One keyring entry per item (service "multichat"), the value is JSON."""

    strict = False  # True: never fall back to a file (MULTICHAT_SECRETS=keyring)

    def __init__(self, keyring) -> None:
        self.kr = keyring
        self.name = f"keyring ({type(keyring).__module__.rsplit('.', 1)[-1]})"

    def load(self, key: str) -> dict | None:
        raw = self.kr.get_password(SERVICE, key)
        return json.loads(raw) if raw else None

    def save(self, key: str, data: dict) -> None:
        self.kr.set_password(SERVICE, key, json.dumps(data))

    def delete(self, key: str) -> None:
        try:
            self.kr.delete_password(SERVICE, key)
        except Exception:  # noqa: BLE001 – not present
            pass


def system_keyring():
    """The usable system keyring or None (also when it is locked and stays locked)."""
    try:
        import keyring  # type: ignore
        from keyring.backends import fail  # type: ignore

        kr = keyring.get_keyring()
        backends = getattr(kr, "backends", [kr])  # ChainerBackend wraps several
        if isinstance(kr, fail.Keyring) or not backends or getattr(kr, "priority", 0) <= 0:
            log.info("Secrets: no system keyring available")
            return None
        kr.get_password(SERVICE, "probe")  # fails if the keyring cannot be opened
        return kr
    except Exception as e:  # noqa: BLE001
        log.warning("Secrets: system keyring not usable: %s", e)
        return None


def choose_backend(directory: Path):
    """→ (backend, file backend for fallback/migration)"""
    files = FileBackend(directory)
    mode = os.environ.get("MULTICHAT_SECRETS", "auto").lower()
    if mode == "file":
        return files, files
    kr = system_keyring()
    if kr is None:
        if mode == "keyring":
            log.error("Secrets: MULTICHAT_SECRETS=keyring, but no keyring is available – logins are kept "
                      "in memory only (nothing is written to a file)")
            return MemoryBackend(), None
        return files, files
    backend = KeyringBackend(kr)
    backend.strict = mode == "keyring"
    return backend, files


class SecretStore:
    """A dict of per-platform dicts, stored as one item (e.g. "tokens" or "apps")."""

    def __init__(self, key: str, backend, files: FileBackend | None = None) -> None:
        self.key = key
        self.backend = backend
        self.files = files if files is not None else (backend if isinstance(backend, FileBackend) else None)
        self.lock = threading.Lock()
        try:
            self.data = backend.load(key) or {}
        except Exception as e:  # noqa: BLE001
            log.warning("Secrets: reading %s from %s failed: %s", key, backend.name, e)
            self.data = {}
        self._migrate_files()

    def _migrate_files(self) -> None:
        """A JSON file next to a keyring: from an older version, or written because the
        keyring failed while the app ran (_save). Either way it is the newer state – it
        replaces what the keyring holds (else a logout made while the keyring failed would
        be undone by the old keyring entry), goes into the keyring and the file is deleted."""
        if not self.files or self.files is self.backend:
            return
        old = self.files.load(self.key)
        if old is None:
            return
        self.data = old if isinstance(old, dict) else {}
        try:
            self.backend.save(self.key, self.data)
            if self.backend.load(self.key) != self.data:
                raise RuntimeError("read-back mismatch")
        except Exception as e:  # noqa: BLE001
            log.warning("Secrets: moving %s into the keyring failed, keeping the file: %s", self.key, e)
            if not getattr(self.backend, "strict", False):
                self.backend = self.files  # keep using the file – it stays the newest state
            return
        self.files.delete(self.key)
        log.info("Secrets: %s moved from %s into the %s", self.key, self.files.path(self.key).name, self.backend.name)

    @property
    def backend_name(self) -> str:
        return self.backend.name

    def all(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.data))

    def get(self, platform: str) -> dict:
        with self.lock:
            return dict(self.data.get(platform) or {})

    def update(self, platform: str, **values) -> bool:
        with self.lock:
            self.data.setdefault(platform, {}).update(values)
            return self._save()

    def replace(self, platform: str, values: dict) -> bool:
        with self.lock:
            self.data[platform] = dict(values)
            return self._save()

    def replace_all(self, data: dict) -> bool:
        with self.lock:
            self.data = json.loads(json.dumps(data))
            return self._save()

    def delete(self, platform: str) -> bool:
        with self.lock:
            self.data.pop(platform, None)
            return self._save()

    def _save(self) -> bool:
        """Whether the new state was saved; memory changes survive a failed save."""
        try:
            self.backend.save(self.key, self.data)
        except Exception as e:  # noqa: BLE001
            if getattr(self.backend, "strict", False):
                # MULTICHAT_SECRETS=keyring: never a file – the change lives in memory until the app closes
                log.error("Secrets: saving %s in the %s failed (%s) – not saved (keyring-only mode)",
                          self.key, self.backend.name, e)
                return False
            if not self.files or self.files is self.backend:
                raise
            # e.g. the wallet was closed: keep the data instead of losing the login
            log.error("Secrets: saving %s in the %s failed (%s) – using the file", self.key, self.backend.name, e)
            try:  # the old entry must not come back at the next start (a logout would be undone)
                self.backend.delete(self.key)
            except Exception:  # noqa: BLE001 – the file wins at the next start anyway (_migrate_files)
                pass
            self.backend = self.files
            self.files.save(self.key, self.data)
        return True


TokenStore = SecretStore  # name used by the accounts

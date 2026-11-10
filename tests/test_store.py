# SPDX-License-Identifier: GPL-3.0-or-later
"""Secret storage: keyring, migration from the old files, fallback."""
import json
import os

import pytest

from multichat.accounts.store import FileBackend, KeyringBackend, SecretStore


class FakeKeyring:
    def __init__(self, fail_writes=False):
        self.items, self.fail_writes = {}, fail_writes

    def get_password(self, service, key):
        return self.items.get((service, key))

    def set_password(self, service, key, value):
        if self.fail_writes:
            raise RuntimeError("wallet closed")
        self.items[(service, key)] = value

    def delete_password(self, service, key):
        self.items.pop((service, key))


def test_file_backend_roundtrip(tmp_path):
    s = SecretStore("tokens", FileBackend(tmp_path))
    s.update("twitch", access_token="x")
    path = tmp_path / "tokens.json"
    assert json.loads(path.read_text()) == {"twitch": {"access_token": "x"}}
    assert SecretStore("tokens", FileBackend(tmp_path)).get("twitch") == {"access_token": "x"}


def test_file_backend_unicode_roundtrip(tmp_path):
    backend = FileBackend(tmp_path)
    data = {"youtube": {"access_token": "synthetic", "user_name": "Grüße 世界"}}
    backend.save("tokens", data)
    assert backend.load("tokens") == data


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions require a POSIX filesystem")
@pytest.mark.parametrize("key", ["tokens", "apps"])
@pytest.mark.parametrize("temporary", ["{key}.tmp", ".{key}.json.tmp"])
def test_file_backend_repairs_private_mode_after_stale_temp(tmp_path, key, temporary):
    # Existing installations can have a readable parent and a leftover temp file
    # with wider permissions. Neither may make newly saved secrets readable.
    tmp_path.chmod(0o755)
    stale = tmp_path / temporary.format(key=key)
    stale.write_text("old non-secret data")
    stale.chmod(0o644)
    backend = FileBackend(tmp_path)
    backend.save(key, {"secret": "synthetic"})
    assert backend.load(key) == {"secret": "synthetic"}
    assert backend.path(key).stat().st_mode & 0o777 == 0o600
    if stale.exists():
        assert "synthetic" not in stale.read_text()


@pytest.mark.skipif(os.name != "posix", reason="POSIX O_NOFOLLOW and symlink creation required")
@pytest.mark.parametrize("temporary", ["tokens.tmp", ".tokens.json.tmp"])
def test_file_backend_never_writes_through_temporary_symlink(tmp_path, temporary):
    backend = FileBackend(tmp_path)
    backend.save("tokens", {"secret": "previous"})
    victim = tmp_path / "unrelated.txt"
    victim.write_text("untouched")
    (tmp_path / temporary).symlink_to(victim)
    if temporary.startswith("."):
        with pytest.raises(OSError):
            backend.save("tokens", {"secret": "new"})
        assert backend.load("tokens") == {"secret": "previous"}
    else:
        backend.save("tokens", {"secret": "new"})
        assert backend.load("tokens") == {"secret": "new"}
    assert victim.read_text() == "untouched"
    assert not backend.path("tokens").is_symlink()


def test_file_backend_keeps_previous_secrets_if_replace_fails(tmp_path, monkeypatch):
    backend = FileBackend(tmp_path)
    backend.save("tokens", {"secret": "previous"})

    def failed_replace(*args):
        raise OSError("synthetic storage failure")

    monkeypatch.setattr(os, "replace", failed_replace)
    with pytest.raises(OSError, match="synthetic storage failure"):
        backend.save("tokens", {"secret": "new"})
    assert backend.load("tokens") == {"secret": "previous"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits require a POSIX filesystem")
def test_file_backend_is_private(tmp_path):
    s = SecretStore("tokens", FileBackend(tmp_path))
    s.update("twitch", access_token="x")
    assert (tmp_path / "tokens.json").stat().st_mode & 0o777 == 0o600


def test_migrates_file_into_keyring(tmp_path):
    files = FileBackend(tmp_path)
    files.save("apps", {"kick": {"client_id": "cid", "client_secret": "sec"}})
    kr = FakeKeyring()
    s = SecretStore("apps", KeyringBackend(kr), files)
    assert s.get("kick")["client_secret"] == "sec"
    assert not (tmp_path / "apps.json").exists()
    assert json.loads(kr.items[("multichat", "apps")])["kick"]["client_id"] == "cid"
    s.delete("kick")
    assert json.loads(kr.items[("multichat", "apps")]) == {}


def test_falls_back_to_file_when_keyring_fails(tmp_path):
    files = FileBackend(tmp_path)
    s = SecretStore("tokens", KeyringBackend(FakeKeyring(fail_writes=True)), files)
    s.update("youtube", access_token="y")  # must not lose the login
    assert json.loads((tmp_path / "tokens.json").read_text())["youtube"]["access_token"] == "y"
    assert s.backend_name == "file"


def test_private_file_replaces_stale_temporary_file_and_complete_json(tmp_path):
    from multichat.core.config import write_private

    target = tmp_path / "tokens.json"
    stale = tmp_path / ".tokens.json.tmp"
    stale.write_text("interrupted earlier write")
    write_private(target, {"token": "first"})
    assert json.loads(target.read_text()) == {"token": "first"}
    assert not stale.exists()
    write_private(target, {"token": "second", "user": "chatter"})
    assert json.loads(target.read_text()) == {"token": "second", "user": "chatter"}
    assert not stale.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions and O_NOFOLLOW require a POSIX filesystem")
def test_private_file_stays_private_and_never_follows_a_symlink(tmp_path):
    """A left-over temp file with wide permissions, or a symlink at its name, must not end
    up as (or redirect) the settings/token file."""
    from multichat.core.config import write_private

    target = tmp_path / "tokens.json"
    stale = tmp_path / ".tokens.json.tmp"
    stale.write_text("old")
    stale.chmod(0o644)
    write_private(target, {"a": 1})
    assert target.stat().st_mode & 0o777 == 0o600 and json.loads(target.read_text()) == {"a": 1}

    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    os.symlink(victim, stale)
    with pytest.raises(OSError):
        write_private(target, {"a": 2})
    assert victim.read_text() == "untouched" and json.loads(target.read_text()) == {"a": 1}

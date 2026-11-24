"""Credential attacks: sign-in hijack, account switch, shared keyring item, launcher.

Attackers: another local user who reads /proc/*/cmdline and connects to 127.0.0.1, a
second nimbo profile (NIMBO_CONFIG_DIR) on the same desktop keyring, and a binary path
that contains "=". Every store here is in memory or a fake Secret Service collection.
"""

from __future__ import annotations

import io
import os
import socket
import stat
import subprocess
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import secretstorage
from rich.console import Console

from nimbo.app import context, wizard
from nimbo.app.i18n import Translator
from nimbo.core import auth
from nimbo.core import secrets as nimbo_secrets
from nimbo.core.auth import OAuthClient, TokenSet
from nimbo.core.config import Config, OAuthConfig, SourceConfig, TargetConfig, save_config
from nimbo.core.errors import AuthError, NimboError
from nimbo.core.index import (
    META_ACCOUNT,
    META_FORCE_FULL,
    META_ROOT_UUID,
    META_TARGET_ID,
    Index,
)
from nimbo.core.paths import Paths
from nimbo.core.safety import write_sentinel
from nimbo.core.secrets import REFRESH_TOKEN_KEY, MemorySecretStore
from tests.fakedrive import FakeDrive

CLIENT_ID = "security.apps.googleusercontent.com"
CLIENT_SECRET = "security-client-secret"
CLIENT = OAuthClient(CLIENT_ID, CLIENT_SECRET)
FAKE_ACCOUNT = "drive-user@example.test"  # what FakeDrive reports for every token
OTHER_ACCOUNT = "mirror-owner@example.test"
KEPT_REFRESH = "kept-refresh-placeholder"
NEW_REFRESH = "new-refresh-placeholder"
NEW_ACCESS = "new-access-placeholder"


@pytest.fixture(autouse=True)
def _no_real_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any accidental use of the real Secret Service fails instead of touching it."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("test tried to reach the real Secret Service")

    monkeypatch.setattr(secretstorage, "dbus_init", refuse)
    monkeypatch.setattr(secretstorage, "get_default_collection", refuse)


def _require_loopback() -> None:
    try:
        with socket.socket(socket.AF_INET) as probe:
            probe.bind(("127.0.0.1", 0))
    except PermissionError as exc:
        pytest.skip(f"IPv4 loopback unavailable in this sandbox: errno {exc.errno}")


def _is_start_url(url: str) -> bool:
    parsed = httpx.URL(url)
    return (parsed.scheme, parsed.host) == ("http", "127.0.0.1") and parsed.path.startswith(
        "/start/"
    )


# CRED1: the consent URL travels only through the loopback start redirect.


@pytest.fixture
def launcher_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Make webbrowser.open use xdg-open, capturing its argv instead of running it."""
    launched: list[list[str]] = []

    def fake_popen(cmdline: list[str], **_kwargs: object) -> SimpleNamespace:
        launched.append(list(cmdline))
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(webbrowser.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(webbrowser, "_tryorder", [], raising=False)
    monkeypatch.setattr(webbrowser, "_browsers", {}, raising=False)
    monkeypatch.setattr(webbrowser, "_os_preferred_browser", None, raising=False)
    webbrowser.register("xdg-open", None, webbrowser.BackgroundBrowser("xdg-open"), preferred=True)
    return launched


def _consent(fake: FakeDrive, browser: httpx.Client, consent_url: str) -> None:
    """Approve consent at the fake and follow Google's redirect to the callback."""
    params = httpx.URL(consent_url).params
    code = fake.oauth.authorize(
        params["client_id"],
        params["redirect_uri"],
        params["code_challenge"],
        params["state"],
        params["scope"],
    )
    page = browser.get(params["redirect_uri"], params={"state": params["state"], "code": code})
    assert page.status_code == 200


def _refused(client: httpx.Client, url: str, **kwargs: Any) -> bool:
    """Return whether the listener refused the request or is already gone."""
    try:
        return client.get(url, **kwargs).status_code >= 400
    except httpx.TransportError:
        return True


def _follow_start(browser: httpx.Client, url: str) -> str:
    assert _is_start_url(url), "the browser must receive a loopback start URL"
    response = browser.get(url)
    assert response.status_code == 302
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    location = response.headers["location"]
    assert location.startswith(auth.AUTH_URL + "?")
    return location


def test_consent_state_and_challenge_never_reach_the_launcher_argv(
    launcher_argv: list[list[str]],
) -> None:
    """The browser launcher sees a single-use loopback URL, not the consent parameters."""
    _require_loopback()
    fake = FakeDrive()
    fake.oauth.register_client(CLIENT_ID, CLIENT_SECRET)
    consent: list[str] = []

    def browser_process() -> None:
        while not launcher_argv:
            threading.Event().wait(0.01)
        url = launcher_argv[0][-1]
        with httpx.Client(trust_env=False, timeout=2) as browser:
            # Before the fix the argv carried the consent URL itself. Never fetch Google.
            consent_url = url if url.startswith(auth.AUTH_URL) else _follow_start(browser, url)
            consent.append(consent_url)
            _consent(fake, browser, consent_url)

    thread = threading.Thread(target=browser_process, daemon=True)
    thread.start()
    with fake.client() as http:
        tokens = auth.authorize_interactive(
            CLIENT, http, timeout_s=10, token_url=fake.TOKEN_URL, auth_url=auth.AUTH_URL
        )
    thread.join(5)
    assert tokens.refresh_token
    (consent_url,) = consent
    params = httpx.URL(consent_url).params
    argv = "\n".join(" ".join(command) for command in launcher_argv)
    assert params["state"] not in argv
    assert params["code_challenge"] not in argv
    assert all(_is_start_url(command[-1]) for command in launcher_argv)


@pytest.fixture
def pending_signin() -> Iterator[tuple[str, Future[TokenSet], FakeDrive]]:
    """Start a sign-in whose browser does nothing, publishing its URL to the test."""
    _require_loopback()
    fake = FakeDrive()
    fake.oauth.register_client(CLIENT_ID, CLIENT_SECRET)
    published: Future[str] = Future()
    with fake.client() as http, ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            auth.authorize_interactive,
            CLIENT,
            http,
            open_browser=lambda _url: True,
            on_url=published.set_result,
            token_url=fake.TOKEN_URL,
            timeout_s=3,
        )
        yield published.result(timeout=2), result, fake


def test_second_start_request_aborts_the_signin(
    pending_signin: tuple[str, Future[TokenSet], FakeDrive],
) -> None:
    """An attacker racing the browser for the start URL causes a retry, not a hijack."""
    url, result, fake = pending_signin
    assert _is_start_url(url)
    with (
        httpx.Client(trust_env=False, timeout=2) as attacker,
        httpx.Client(trust_env=False, timeout=2) as browser,
    ):
        consent_url = _follow_start(attacker, url)
        repeated = browser.get(url)
        assert repeated.status_code >= 400 and "location" not in repeated.headers
        params = httpx.URL(consent_url).params
        assert params["state"] not in repeated.text
        code = fake.oauth.authorize(
            params["client_id"],
            params["redirect_uri"],
            params["code_challenge"],
            params["state"],
            params["scope"],
        )
        # The sign-in is already over; the callback is refused or the listener gone.
        assert _refused(
            attacker, params["redirect_uri"], params={"state": params["state"], "code": code}
        )
    with pytest.raises(AuthError) as caught:
        result.result(timeout=5)
    assert caught.value.code == auth.INTERCEPTED


def test_callback_before_start_aborts_the_signin(
    pending_signin: tuple[str, Future[TokenSet], FakeDrive],
) -> None:
    """A callback that arrives before the start redirect was served ends the sign-in."""
    url, result, _ = pending_signin
    assert _is_start_url(url)
    callback = str(httpx.URL(url).copy_with(path="/"))
    with httpx.Client(trust_env=False, timeout=2) as attacker:
        assert _refused(attacker, callback, params={"state": "guessed", "code": "attacker-code"})
        assert _refused(attacker, url)
    with pytest.raises(AuthError) as caught:
        result.result(timeout=5)
    assert caught.value.code == auth.INTERCEPTED


def _closed(connection: socket.socket) -> bool:
    try:
        return connection.recv(1) == b""
    except ConnectionResetError:
        return True


def test_connection_cap_and_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connections beyond the cap close at once, and slow drips end at the total deadline."""
    _require_loopback()
    monkeypatch.setattr(auth, "_MAX_CONNECTIONS", 2)
    monkeypatch.setattr(auth, "_CONNECTION_DEADLINE", 0.6)
    with auth._callback_server("s" * 43, "n" * 43, float("inf")) as server:
        address = ("127.0.0.1", server.server_port)
        slow = [socket.create_connection(address, timeout=5) for _ in range(2)]
        try:
            threading.Event().wait(0.2)
            began = time.monotonic()
            with socket.create_connection(address, timeout=5) as extra:
                assert _closed(extra)  # refused without a handler thread
            assert time.monotonic() - began < 1.0
            # Drip one header byte at a time, well within the 2 s per-read timeout.
            cut: set[int] = set()
            began = time.monotonic()
            while len(cut) < len(slow) and time.monotonic() - began < 1.8:
                for number, connection in enumerate(slow):
                    if number in cut:
                        continue
                    try:
                        connection.sendall(b"G")
                    except OSError:
                        cut.add(number)
                threading.Event().wait(0.1)
            assert len(cut) == len(slow), "slow connections outlived the total deadline"
        finally:
            for connection in slow:
                connection.close()


# CRED2: the Google account is bound to the mirror.


def _profile(tmp_path: Path, *, account: str | None) -> tuple[Paths, Config]:
    paths = Paths(tmp_path / "config", tmp_path / "state")
    paths.ensure()
    root = tmp_path / "source"
    (root / "Docs").mkdir(parents=True)
    cfg = Config(
        SourceConfig(root, ("Docs",), require_mount=False),
        TargetConfig("target-folder-0001"),
        OAuthConfig(CLIENT_ID, CLIENT_SECRET),
    )
    save_config(cfg, paths.config_file)
    with closing(Index(paths.db_file)) as index:
        index.set_meta(META_ACCOUNT, account)
    return paths, cfg


@dataclass
class _Grant:
    store: MemorySecretStore
    prompts: list[str]
    target: str


@pytest.fixture
def new_grant(monkeypatch: pytest.MonkeyPatch) -> _Grant:
    """Wire the wizard to the Drive fake and a sign-in that returns a fresh grant."""
    fake = FakeDrive()
    fake.oauth.add_access_token(NEW_ACCESS)
    store = MemorySecretStore()
    store.set(REFRESH_TOKEN_KEY, KEPT_REFRESH, label="nimbo")
    prompts: list[str] = []
    monkeypatch.setattr(context, "secret_store", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(context, "new_http_client", lambda **_kwargs: fake.client())
    monkeypatch.setattr(
        wizard.auth,
        "authorize_interactive",
        lambda *_args, **_kwargs: TokenSet(NEW_ACCESS, NEW_REFRESH, 4e9, auth.SCOPE),
    )
    return _Grant(store, prompts, fake.add_folder("Mirror"))


def _stdin(monkeypatch: pytest.MonkeyPatch, *, tty: bool) -> None:
    stream = io.StringIO()
    stream.isatty = lambda: tty  # type: ignore[method-assign]
    monkeypatch.setattr(wizard.sys, "stdin", stream)


def _decline(prompts: list[str]) -> Callable[..., bool]:
    def answer(_t: Translator, _console: Console, message: str, **_kwargs: Any) -> bool:
        prompts.append(message)
        return False

    return answer


def test_reconnect_refuses_a_different_account_and_keeps_the_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    new_grant: _Grant,
) -> None:
    store = new_grant.store
    paths, cfg = _profile(tmp_path, account=OTHER_ACCOUNT)
    _stdin(monkeypatch, tty=False)
    out = io.StringIO()
    with pytest.raises(NimboError) as caught:
        wizard.reconnect(
            paths, cfg, wizard.ClientOptions(), t=Translator("en"), console=Console(file=out)
        )
    assert caught.value.code == "wizard.account_mismatch"
    assert store.get(REFRESH_TOKEN_KEY) == KEPT_REFRESH
    assert "backup install" in Translator("en")(caught.value.code)
    assert "backup instalar" in Translator("es")(caught.value.code)


def test_reconnect_same_account_unattended_prints_it_without_asking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    new_grant: _Grant,
) -> None:
    store, prompts = new_grant.store, new_grant.prompts
    paths, cfg = _profile(tmp_path, account=FAKE_ACCOUNT)
    _stdin(monkeypatch, tty=False)
    monkeypatch.setattr(wizard, "confirm", _decline(prompts))
    out = io.StringIO()
    assert (
        wizard.reconnect(
            paths, cfg, wizard.ClientOptions(), t=Translator("en"), console=Console(file=out)
        )
        == 0
    )
    assert prompts == [] and FAKE_ACCOUNT in out.getvalue()
    assert store.get(REFRESH_TOKEN_KEY) == NEW_REFRESH


def test_declining_the_account_on_reconnect_stores_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    new_grant: _Grant,
) -> None:
    store, prompts = new_grant.store, new_grant.prompts
    paths, cfg = _profile(tmp_path, account=FAKE_ACCOUNT)
    _stdin(monkeypatch, tty=True)
    monkeypatch.setattr(wizard, "confirm", _decline(prompts))
    out = io.StringIO()
    result = wizard.reconnect(
        paths, cfg, wizard.ClientOptions(), t=Translator("en"), console=Console(file=out)
    )
    assert result != 0
    assert len(prompts) == 1 and FAKE_ACCOUNT in prompts[0]
    assert store.get(REFRESH_TOKEN_KEY) == KEPT_REFRESH


def test_declining_the_account_on_install_stores_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    new_grant: _Grant,
) -> None:
    store, prompts = new_grant.store, new_grant.prompts
    store.delete(REFRESH_TOKEN_KEY)
    paths = Paths.default()
    root = tmp_path / "source"
    (root / "Docs").mkdir(parents=True)
    _stdin(monkeypatch, tty=True)
    monkeypatch.setattr(wizard, "confirm", _decline(prompts))
    options = wizard.InstallOptions(
        client=wizard.ClientOptions(client_id=CLIENT_ID, client_secret=CLIENT_SECRET),
        source=root,
        folders="Docs",
        target=new_grant.target,
        at="03:00",
        no_service=True,
    )
    out = io.StringIO()
    result = wizard.install(
        paths,
        options,
        t=Translator("en"),
        console=Console(file=out),
        preview=lambda: 0,
    )
    assert result != 0
    assert len(prompts) == 1 and FAKE_ACCOUNT in prompts[0]
    assert store.get(REFRESH_TOKEN_KEY) is None
    assert not paths.config_file.exists()
    assert not (root / ".nimbo-root").exists()


def test_install_with_a_different_journal_account_resets_the_mirror(tmp_path: Path) -> None:
    """A new account cannot inherit the remote state recorded for another account."""
    paths, cfg = _profile(tmp_path, account=OTHER_ACCOUNT)
    write_sentinel(cfg.source.root, "root-uuid-0001")
    with closing(Index(paths.db_file)) as index:
        index.set_meta(META_ROOT_UUID, "root-uuid-0001")
        index.set_meta(META_TARGET_ID, cfg.target.folder_id)
        index.set_meta(META_FORCE_FULL, None)
    wizard._save_setup(paths, cfg, cfg, MemorySecretStore(), None, FAKE_ACCOUNT)
    with closing(Index.open_readonly(paths.db_file)) as index:
        assert index.get_meta(META_ACCOUNT) == FAKE_ACCOUNT
        assert index.get_meta(META_FORCE_FULL) == "1"


# CRED3: each profile has its own keyring item.


@dataclass
class _Item:
    collection: _Collection
    item_path: str
    attributes: dict[str, str]
    label: str
    secret: bytes = field(repr=False)
    created: int

    def get_secret(self) -> bytes:
        return self.secret

    def get_created(self) -> int:
        return self.created

    def get_label(self) -> str:
        return self.label

    def get_attributes(self) -> dict[str, str]:
        return dict(self.attributes)

    def delete(self) -> None:
        self.collection.items.remove(self)


class _Collection:
    """One shared in-memory default collection with Secret Service subset matching."""

    def __init__(self) -> None:
        self.items: list[_Item] = []
        self.serial = 0

    def is_locked(self) -> bool:
        return False

    def search_items(self, attributes: dict[str, str]) -> Iterator[_Item]:
        return (item for item in list(self.items) if attributes.items() <= item.attributes.items())

    def create_item(
        self, label: str, attributes: dict[str, str], secret: bytes, replace: bool = False
    ) -> _Item:
        if replace:
            for item in self.search_items(attributes):
                if item.attributes == attributes:
                    item.label, item.secret = label, secret
                    return item
        self.serial += 1
        item = _Item(self, f"/item/{self.serial}", dict(attributes), label, secret, self.serial)
        self.items.append(item)
        return item


class _Bus:
    def close(self) -> None:
        pass


@pytest.fixture
def desktop_keyring(monkeypatch: pytest.MonkeyPatch) -> _Collection:
    collection = _Collection()
    monkeypatch.setattr(nimbo_secrets.secretstorage, "dbus_init", _Bus)
    monkeypatch.setattr(
        nimbo_secrets.secretstorage, "get_default_collection", lambda _bus: collection
    )
    return collection


def _profile_paths(base: Path) -> Paths:
    paths = Paths(base / "config", base / "state")
    paths.ensure()
    return paths


def test_profiles_keep_separate_credentials_and_purge_only_their_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop_keyring: _Collection
) -> None:
    paths_a, paths_b = _profile_paths(tmp_path / "a"), _profile_paths(tmp_path / "b")
    hooks = context.TestHooks()
    context.secret_store(hooks, paths_a.config_dir).set(REFRESH_TOKEN_KEY, "a-value", label="n")
    context.secret_store(hooks, paths_b.config_dir).set(REFRESH_TOKEN_KEY, "b-value", label="n")
    assert context.secret_store(hooks, paths_a.config_dir).get(REFRESH_TOKEN_KEY) == "a-value"
    assert context.secret_store(hooks, paths_b.config_dir).get(REFRESH_TOKEN_KEY) == "b-value"

    revoked: list[str] = []
    monkeypatch.setattr(
        wizard.auth, "revoke", lambda token, _http, **_kwargs: revoked.append(token)
    )
    monkeypatch.setattr(wizard.service, "units_dir", lambda: tmp_path / "units")
    assert (
        wizard.uninstall(
            paths_a,
            purge=True,
            assume_yes=True,
            revoke=True,
            t=Translator("en"),
            console=Console(file=io.StringIO()),
        )
        == 0
    )
    assert revoked == ["a-value"]
    assert context.secret_store(hooks, paths_a.config_dir).get(REFRESH_TOKEN_KEY) is None
    assert context.secret_store(hooks, paths_b.config_dir).get(REFRESH_TOKEN_KEY) == "b-value"


def test_profile_attribute_is_a_hash_of_the_resolved_config_dir(
    tmp_path: Path, desktop_keyring: _Collection
) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    profile = nimbo_secrets.profile_id(tmp_path / "real")
    assert len(profile) == 32 and int(profile, 16) >= 0
    assert nimbo_secrets.profile_id(tmp_path / "link") == profile
    assert nimbo_secrets.profile_id(tmp_path / "other") != profile
    store = context.secret_store(context.TestHooks(), tmp_path / "link")
    store.set(REFRESH_TOKEN_KEY, "value", label="n")
    (item,) = desktop_keyring.items
    assert item.attributes == {
        "application": "nimbo",
        "key": REFRESH_TOKEN_KEY,
        "profile": profile,
    }


def test_a_single_legacy_item_migrates_into_the_first_profile(
    tmp_path: Path, desktop_keyring: _Collection
) -> None:
    legacy = {"application": "nimbo", "key": REFRESH_TOKEN_KEY}
    desktop_keyring.create_item("nimbo: legacy", legacy, b"legacy-value")
    hooks = context.TestHooks()
    store_a = context.secret_store(hooks, tmp_path / "a")
    assert store_a.get(REFRESH_TOKEN_KEY) == "legacy-value"
    (item,) = desktop_keyring.items
    assert item.attributes == {**legacy, "profile": nimbo_secrets.profile_id(tmp_path / "a")}
    assert item.label == "nimbo: legacy"
    assert store_a.get(REFRESH_TOKEN_KEY) == "legacy-value"
    assert context.secret_store(hooks, tmp_path / "b").get(REFRESH_TOKEN_KEY) is None
    assert len(desktop_keyring.items) == 1


def test_ambiguous_legacy_items_are_not_adopted(
    tmp_path: Path, desktop_keyring: _Collection
) -> None:
    legacy = {"application": "nimbo", "key": REFRESH_TOKEN_KEY}
    desktop_keyring.create_item("one", legacy, b"first")
    desktop_keyring.create_item("two", dict(legacy), b"second")
    store = context.secret_store(context.TestHooks(), tmp_path / "a")
    assert store.get(REFRESH_TOKEN_KEY) is None
    store.delete(REFRESH_TOKEN_KEY)
    assert [item.secret for item in desktop_keyring.items] == [b"first", b"second"]


# SYS6: the launcher runs the configured binary even when its path contains "=".


def test_launcher_runs_the_binary_whose_path_contains_equals(tmp_path: Path) -> None:
    record = tmp_path / "record.txt"
    folder = tmp_path / "bin=evil dir"
    folder.mkdir()
    binary = folder / "backup"
    binary.write_text(
        '#!/bin/sh\nprintf "%s|%s|%s\\n" "$NIMBO_CONFIG_DIR" "$NIMBO_STATE_DIR" "$*" '
        f'> "{record}"\n',
        encoding="utf-8",
    )
    binary.chmod(0o700)
    # Whatever the first argument names in PATH must never run instead of the binary.
    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    decoy = decoy_dir / "ejecutar"
    decoy.write_text(f'#!/bin/sh\necho hijacked > "{record}"\n', encoding="utf-8")
    decoy.chmod(0o700)

    paths = Paths(tmp_path / "conf 'x'=y", tmp_path / "state $HOME")
    paths.ensure()
    launcher = wizard._launcher(paths, binary)
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o700
    env = {"PATH": f"{decoy_dir}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(tmp_path)}
    completed = subprocess.run(
        [str(launcher), "ejecutar", "--auto"], env=env, capture_output=True, timeout=10
    )
    assert completed.returncode == 0, completed.stderr
    assert record.read_text(encoding="utf-8") == (
        f"{paths.config_dir.absolute()}|{paths.state_dir.absolute()}|ejecutar --auto\n"
    )

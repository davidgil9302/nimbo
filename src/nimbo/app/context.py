"""Assemble app dependencies and restrict integration hooks to loopback origins."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from nimbo import __version__
from nimbo.core.auth import OAuthClient, RefreshingTokenProvider, StaticTokenProvider
from nimbo.core.config import Config, OAuthConfig, SourceConfig, TransferConfig
from nimbo.core.drive import DEFAULT_API_BASE, DriveClient, TokenProvider
from nimbo.core.engine import Engine, EngineDeps, Notifier
from nimbo.core.errors import NimboError
from nimbo.core.index import Index
from nimbo.core.paths import Paths
from nimbo.core.ratelimit import RateLimiter, RetryPolicy
from nimbo.core.safety import network_available
from nimbo.core.secrets import MemorySecretStore, SecretServiceStore, SecretStore, profile_id
from nimbo.core.transfer import Downloader, Uploader

from . import service
from .i18n import Translator, detect_language

_LOG = logging.getLogger(__name__)
_LOOPBACK = re.compile(r"http://(?:127\.0\.0\.1|localhost):([0-9]{1,5})")
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


@dataclass(frozen=True, slots=True)
class TestHooks:
    """Optional local Drive and OAuth origins and a fixed test bearer token."""

    api_base: str | None = None
    token: str | None = field(default=None, repr=False)
    oauth_base: str | None = None


def _is_loopback(value: str | None) -> bool:
    match = _LOOPBACK.fullmatch(value) if value is not None else None
    return match is not None and 1 <= int(match[1]) <= 65535


def test_hooks(env: Mapping[str, str]) -> TestHooks:
    """Read test overrides only when the Drive origin is explicitly HTTP loopback."""
    base, token, oauth = (
        env.get(f"NIMBO_{key}") for key in ("API_BASE", "TEST_TOKEN", "OAUTH_BASE")
    )
    if not _is_loopback(base):
        if any(value is not None for value in (base, token, oauth)):
            t = Translator(detect_language(None, env))
            _LOG.warning(t("cli.hooks_ignored"))
        return TestHooks()
    if oauth is not None and not _is_loopback(oauth):
        t = Translator(detect_language(None, env))
        _LOG.warning(t("cli.oauth_hook_ignored"))
        oauth = None
    return TestHooks(base, token, oauth)


def new_http_client(*, loopback: bool = False) -> httpx.Client:
    """Return the caller-owned HTTP/1.1 pool, bypassing proxies for loopback hooks."""
    return httpx.Client(
        timeout=httpx.Timeout(connect=30, read=300, write=300, pool=60),
        headers={"User-Agent": f"nimbo/{__version__}"},
        http2=False,
        trust_env=not loopback,
    )


def secret_store(hooks: TestHooks, config_dir: Path | None = None) -> SecretStore:
    """Select in-memory storage in hook mode and the profile's keyring item otherwise.

    The profile is the configuration directory, which defaults to the one the
    environment selects. Callers that already hold their Paths pass its config_dir.
    """
    if hooks.api_base is not None:
        return MemorySecretStore()
    directory = Paths.default().config_dir if config_dir is None else config_dir
    return SecretServiceStore(profile=profile_id(directory))


def token_provider(
    oauth: OAuthConfig, store: SecretStore, http: httpx.Client, hooks: TestHooks
) -> TokenProvider:
    """Return the token provider for the configured OAuth client or a loopback test."""
    if hooks.api_base is not None:
        return StaticTokenProvider(hooks.token or "")
    return RefreshingTokenProvider(OAuthClient(oauth.client_id, oauth.client_secret), store, http)


def make_drive(
    http: httpx.Client, tokens: TokenProvider, transfer: TransferConfig, hooks: TestHooks
) -> DriveClient:
    """Apply the configured read and write budgets to a caller-owned Drive pool."""
    limiter = RateLimiter(
        reads_per_second=transfer.reads_per_second, writes_per_second=transfer.writes_per_second
    )
    return DriveClient(
        http, tokens, limiter, RetryPolicy(), api_base=hooks.api_base or DEFAULT_API_BASE
    )


def _network_via_drive() -> bool:
    # The local client itself checks reachability, without probing Google's host.
    return True


def validate_state_paths(paths: Paths) -> None:
    """Reject symlinks at writable state paths, including SQLite's derived files."""
    locations = (
        paths.lock_file,
        paths.log_dir,
        paths.db_file,
        *(paths.db_file.with_name(paths.db_file.name + suffix) for suffix in SQLITE_SIDECARS),
    )
    if any(path.is_symlink() for path in locations):
        raise NimboError(code="cli.unsafe_path")


def validate_source_scope(source: SourceConfig, paths: Paths) -> None:
    """Reject source folders that overlap Nimbo's config, state, or units."""
    root = source.root.resolve()
    protected = (
        paths.config_dir.resolve(),
        paths.state_dir.resolve(),
        service.units_dir().resolve(),
    )
    for name in source.folders:
        folder = root / name
        if any(folder.is_relative_to(path) or path.is_relative_to(folder) for path in protected):
            raise NimboError(code="wizard.source_contains_state")


def safe_label(value: object) -> str:
    """Preserve visible text and escape terminal controls in filenames and labels."""
    return "".join(
        char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
        for char in str(value)
    )


def build_engine(
    paths: Paths,
    cfg: Config,
    *,
    env: Mapping[str, str] | None = None,
    notifier: Notifier | None = None,
) -> tuple[Engine, Callable[[], None]]:
    """Build the engine and return its resource closer. Propagate setup and I/O errors."""
    env = os.environ if env is None else env
    hooks = test_hooks(env)
    t = Translator(detect_language(cfg.language, env))
    validate_state_paths(paths)
    validate_source_scope(cfg.source, paths)
    paths.ensure()
    with ExitStack() as stack:
        http = stack.enter_context(new_http_client(loopback=hooks.api_base is not None))
        index = Index(paths.db_file)
        stack.callback(index.close)
        drive = make_drive(
            http,
            token_provider(cfg.oauth, secret_store(hooks, paths.config_dir), http, hooks),
            cfg.transfer,
            hooks,
        )
        engine = Engine(
            EngineDeps(
                cfg=cfg,
                paths=paths,
                index=index,
                drive=drive,
                uploader=Uploader(
                    drive,
                    index,
                    chunk_size=cfg.transfer.chunk_mib << 20,
                    multipart_max=cfg.transfer.multipart_max_mib << 20,
                ),
                downloader=Downloader(drive),
                notifier=service.DesktopNotifier(t) if notifier is None else notifier,
                network_check=_network_via_drive
                if hooks.api_base is not None
                else network_available,
                idle_inhibitor=lambda: service.idle_inhibitor(t("cli.inhibit")),
            )
        )
        return engine, stack.pop_all().close

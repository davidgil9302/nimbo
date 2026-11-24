"""Guide installation, reconnect OAuth, and remove the selected local data."""

from __future__ import annotations

import errno
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import webbrowser
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from uuid import uuid4

import httpx
from rich.console import Console
from rich.prompt import Prompt
from rich.text import Text

from nimbo.core import auth, safety
from nimbo.core.config import (
    DEFAULT_EXCLUDES,
    Config,
    OAuthConfig,
    ScheduleConfig,
    SourceConfig,
    TargetConfig,
    TransferConfig,
    load_config,
    parse_folder_id,
    save_config,
)
from nimbo.core.drive import DriveClient
from nimbo.core.errors import AuthError, ConfigError, NimboError
from nimbo.core.fsutil import atomic_write_text
from nimbo.core.index import (
    META_ACCOUNT,
    META_CHANGES_PROBE,
    META_CHANGES_RELIABLE,
    META_CHANGES_TOKEN,
    META_FORCE_FULL,
    META_LAST_DEEP,
    META_LAST_FULL,
    META_QUOTA_LIMIT,
    META_QUOTA_USED,
    META_ROOT_UUID,
    META_TARGET_ID,
    Index,
)
from nimbo.core.lock import RunLock
from nimbo.core.model import nfc
from nimbo.core.paths import Paths
from nimbo.core.secrets import REFRESH_TOKEN_KEY, SecretStore

from . import context, service
from .context import safe_label
from .i18n import Translator

_GUIDE = "docs/oauth-client.md"
_GOOGLE_CREDENTIALS = "https://console.cloud.google.com/apis/credentials"
_CLIENT = re.compile(r"[A-Za-z0-9_-]+\.apps\.googleusercontent\.com")
_SECRET = re.compile(r"[A-Za-z0-9_-]+")
_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
_LAUNCHER = "systemd-launch.sh"
META_UNDO_BOUNDARY = "cli_undo_boundary"
_LOG_FILE = re.compile(r"nimbo-[0-9]{4}-[0-9]{2}-[0-9]{2}\.jsonl")


@dataclass(frozen=True, slots=True)
class ClientOptions:
    """Supply a downloaded desktop-client JSON or the ID and secret explicitly."""

    client_json: Path | None = None
    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)

    @property
    def provided(self) -> bool:
        """Return whether at least one credential option was specified."""
        return any(
            value is not None for value in (self.client_json, self.client_id, self.client_secret)
        )


@dataclass(frozen=True, slots=True)
class InstallOptions:
    """Represent setup choices supplied on the command line."""

    client: ClientOptions = ClientOptions()
    source: Path | None = None
    folders: str | None = None
    target: str | None = None
    at: str | None = None
    unattended: bool = False
    no_service: bool = False
    assume_yes: bool = False


def confirm(t: Translator, console: Console, message: str, *, stream: TextIO | None = None) -> bool:
    """Ask for a localized yes or no, defaulting to no if input closes."""
    yes, no = t("cli.answer_yes"), t("cli.answer_no")
    try:
        return bool(
            Prompt.ask(
                Text(t("cli.confirm_choice", message=message)),
                console=console,
                choices=[yes, no],
                case_sensitive=False,
                default=no,
                stream=stream,
                show_default=False,
                show_choices=False,
            )
            == yes
        )
    except EOFError:
        return False


def _existing_config(paths: Paths) -> Config | None:
    if not os.path.lexists(paths.config_file):
        return None
    return load_config(paths.config_file)


def _validate_client(client_id: object, secret: object) -> OAuthConfig:
    if not isinstance(client_id, str) or not _CLIENT.fullmatch(client_id):
        raise ConfigError(code="wizard.client_id_invalid")
    if not isinstance(secret, str) or not _SECRET.fullmatch(secret):
        raise ConfigError(code="wizard.client_secret_invalid")
    return OAuthConfig(client_id, secret)


def _read_client(path: Path) -> OAuthConfig:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        installed = data["installed"]
        return _validate_client(installed["client_id"], installed["client_secret"])
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise ConfigError(code="wizard.client_json_invalid") from None


def _resolve_client(
    options: ClientOptions, t: Translator, console: Console, *, unattended: bool
) -> OAuthConfig:
    if options.client_json is not None:
        if options.client_id is not None or options.client_secret is not None:
            raise ConfigError(code="wizard.client_options_conflict")
        return _read_client(options.client_json)
    client_id, secret = options.client_id, options.client_secret
    if not unattended and (client_id is None or secret is None):
        console.print(Text(t("wizard.guide", path=_GUIDE)))
        if confirm(t, console, t("wizard.open_guide")):
            guide = Path(__file__).resolve().parents[3] / _GUIDE
            webbrowser.open(guide.as_uri() if guide.is_file() else _GOOGLE_CREDENTIALS)
        if client_id is None:
            client_id = Prompt.ask(Text(t("wizard.client_id")), console=console).strip()
        if secret is None:
            secret = Prompt.ask(Text(t("wizard.client_secret")), console=console, password=True)
    return _validate_client(client_id, secret)


def _folders_at(root: Path) -> list[str]:
    try:
        return sorted(entry.name for entry in root.iterdir() if stat.S_ISDIR(entry.lstat().st_mode))
    except OSError:
        raise ConfigError(code="reason.source_missing") from None


def _get_source(
    options: InstallOptions,
    previous: SourceConfig | None,
    t: Translator,
    console: Console,
) -> SourceConfig:
    if (
        previous is not None
        and options.source is None
        and options.folders is None
        and (
            options.unattended
            or not confirm(t, console, t("wizard.replace_source", root=safe_label(previous.root)))
        )
    ):
        return previous
    source = options.source
    if source is None:
        if options.unattended:
            if previous is None:
                raise ConfigError(code="wizard.source_required")
            source = previous.root
        else:
            prompt = Text(t("wizard.source"))
            value = (
                Prompt.ask(prompt, console=console)
                if previous is None
                else Prompt.ask(prompt, console=console, default=str(previous.root))
            )
            if not value:
                raise ConfigError(code="wizard.source_required")
            source = Path(value)
    root = source.expanduser().resolve()
    available = _folders_at(root)
    if options.folders is not None:
        names = tuple(part.strip() for part in options.folders.split(","))
    elif options.unattended:
        if previous is None:
            raise ConfigError(code="wizard.folders_required")
        names = previous.folders
    else:
        for number, name in enumerate(available, 1):
            console.print(Text(f"{number}. {safe_label(name)}"))
        indices = Prompt.ask(Text(t("wizard.folders")), console=console)
        try:
            numbers = [int(part) for part in indices.split(",")]
            if any(not 1 <= number <= len(available) for number in numbers):
                raise ValueError
            names = tuple(available[number - 1] for number in numbers)
        except ValueError:
            raise ConfigError(code="wizard.folders_invalid") from None
    if (
        not names
        or len(set(names)) != len(names)
        or any(name not in available or name != nfc(name) for name in names)
    ):
        raise ConfigError(code="wizard.folders_invalid")
    same_root = previous is not None and previous.root == root
    return SourceConfig(
        root,
        names,
        tuple(name for name in previous.paused if name in names) if same_root and previous else (),
        previous.require_mount if same_root and previous else root.is_mount(),
    )


def _get_target(
    options: InstallOptions, previous: TargetConfig | None, t: Translator, console: Console
) -> str:
    if options.target is not None:
        return parse_folder_id(options.target)
    if previous is not None and (
        options.unattended
        or not confirm(
            t,
            console,
            t("wizard.replace_target", target=safe_label(previous.label or previous.folder_id)),
        )
    ):
        return previous.folder_id
    if options.unattended:
        raise ConfigError(code="wizard.target_required")
    return parse_folder_id(Prompt.ask(Text(t("wizard.target")), console=console))


def _get_time(
    options: InstallOptions, previous: Config | None, t: Translator, console: Console
) -> str:
    old = previous.schedule.daily_at if previous else ScheduleConfig().daily_at
    if options.at is not None:
        value = options.at
    elif options.unattended or (
        previous and not confirm(t, console, t("wizard.replace_time", time=old))
    ):
        value = old
    else:
        value = str(Prompt.ask(Text(t("wizard.time")), console=console, default=old))
    if not _TIME.fullmatch(value):
        raise ConfigError(code="wizard.time_invalid")
    return value


def _service_available() -> bool:
    try:
        return service.run_systemctl(["show-environment"]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _service_executable() -> Path:
    found = shutil.which("backup") or shutil.which("nimbo")
    sibling = Path(sys.executable).parent / "backup"
    binary = str(sibling) if sibling.is_file() else found
    if binary is None:
        raise ConfigError(code="wizard.command_missing")
    return Path(binary).absolute()


def _launcher(paths: Paths, binary: Path) -> Path:
    launcher = paths.config_dir / _LAUNCHER
    # Plain exports keep a binary path containing "=" from turning into an env assignment.
    command = (
        "#!/bin/sh\n"
        f"export NIMBO_CONFIG_DIR={shlex.quote(str(paths.config_dir.absolute()))}\n"
        f"export NIMBO_STATE_DIR={shlex.quote(str(paths.state_dir.absolute()))}\n"
        f'exec {shlex.quote(str(binary))} "$@"\n'
    )
    atomic_write_text(launcher, command, mode=0o700)
    return launcher


def _setup_service(
    options: InstallOptions,
    previous: Config | None,
    daily_at: str,
    t: Translator,
    console: Console,
) -> bool:
    if options.no_service:
        return False
    state = service.service_state()
    if (
        state.installed
        and state.timer_enabled
        and previous
        and previous.schedule.daily_at == daily_at
    ):
        return False
    if _service_available():
        return (
            options.unattended
            or not state.installed
            or confirm(t, console, t("wizard.replace_units"))
        )
    if not options.unattended:
        console.print(Text(t("wizard.service_unavailable"), style="yellow"))
        if confirm(t, console, t("wizard.continue_without_units")):
            return False
    raise ConfigError(code="wizard.service_unavailable")


def _drive_session(
    oauth: OAuthConfig,
    transfer: TransferConfig,
    http: httpx.Client,
    hooks: context.TestHooks,
    store: SecretStore,
    t: Translator,
    console: Console,
    *,
    authorize: bool,
) -> tuple[DriveClient, str | None]:
    tokens = context.token_provider(oauth, store, http, hooks)
    if hooks.api_base is not None or not authorize:
        return context.make_drive(http, tokens, transfer, hooks), None
    console.print(Text(t("wizard.browser")))
    grant = auth.authorize_interactive(auth.OAuthClient(oauth.client_id, oauth.client_secret), http)
    if not grant.refresh_token:
        raise AuthError()
    return context.make_drive(
        http, auth.StaticTokenProvider(grant.access_token), transfer, hooks
    ), grant.refresh_token


@contextmanager
def _store_refresh(store: SecretStore, refresh: str | None) -> Iterator[None]:
    if refresh is None:
        yield
        return
    previous = store.get(REFRESH_TOKEN_KEY)
    try:
        store.set(REFRESH_TOKEN_KEY, refresh, label="nimbo")
        yield
    except BaseException:
        if previous is None:
            store.delete(REFRESH_TOKEN_KEY)
        else:
            store.set(REFRESH_TOKEN_KEY, previous, label="nimbo")
        raise


def _recorded_account(paths: Paths) -> str | None:
    """Return the Google account the journal records for this mirror, if any."""
    if not paths.db_file.is_file():
        return None
    with closing(Index.open_readonly(paths.db_file)) as index:
        return index.get_meta(META_ACCOUNT)


def _confirm_account(t: Translator, console: Console, account: str | None) -> bool:
    name = safe_label(account or t("common.unknown"))
    return confirm(t, console, t("wizard.confirm_account", name=name))


def _setup_complete(paths: Paths, cfg: Config) -> bool:
    if not paths.db_file.is_file():
        return False
    marker = safety.read_sentinel(cfg.source.root)
    with closing(Index.open_readonly(paths.db_file)) as index:
        root_uuid = index.get_meta(META_ROOT_UUID)
        if root_uuid and root_uuid != marker:
            raise NimboError(code="sentinel_mismatch")
        return bool(root_uuid and index.get_meta(META_TARGET_ID) == cfg.target.folder_id)


def _save_setup(
    paths: Paths,
    cfg: Config,
    previous: Config | None,
    store: SecretStore,
    refresh: str | None,
    account: str | None,
) -> None:
    context.validate_state_paths(paths)
    paths.ensure()
    with RunLock(paths.lock_file), closing(Index(paths.db_file)) as index:
        if _existing_config(paths) != previous:
            raise NimboError(code="wizard.setup_changed")
        source = cfg.source.root
        marker = safety.read_sentinel(source)
        recorded_root = index.get_meta(META_ROOT_UUID)
        if (
            previous
            and previous.source.root == source
            and recorded_root
            and recorded_root != marker
        ):
            raise NimboError(code="sentinel_mismatch")
        root_uuid = marker or str(uuid4())
        changed_mirror = root_uuid != recorded_root or cfg.target.folder_id != index.get_meta(
            META_TARGET_ID
        )
        recorded_account = index.get_meta(META_ACCOUNT)
        changed_account = (previous is not None and cfg.oauth != previous.oauth) or (
            recorded_account is not None and recorded_account != account
        )
        reset = changed_mirror or changed_account
        if reset and index.inflight_ops():
            raise NimboError(code="wizard.unfinished_run")
        safety.write_sentinel(source, root_uuid)
        last_trash = index.last_run_with_trash() if changed_mirror else None
        if reset or (previous is not None and cfg.source != previous.source):
            paths.approval_file.unlink(missing_ok=True)
        saved = False
        with _store_refresh(store, refresh):
            try:
                if cfg != previous:
                    save_config(cfg, paths.config_file)
                    saved = True
                if reset:
                    index.prune_sessions(datetime.max.replace(tzinfo=UTC).isoformat())
                    index.prune_hashes(keep=())
                    meta = {
                        META_ROOT_UUID: root_uuid,
                        META_TARGET_ID: cfg.target.folder_id,
                        META_ACCOUNT: account,
                        META_QUOTA_USED: None,
                        META_QUOTA_LIMIT: None,
                        META_CHANGES_TOKEN: None,
                        META_CHANGES_PROBE: None,
                        META_CHANGES_RELIABLE: None,
                        META_LAST_FULL: None,
                        META_LAST_DEEP: None,
                        META_FORCE_FULL: "1",
                    }
                    if last_trash is not None:
                        meta[META_UNDO_BOUNDARY] = str(last_trash)
                    index.replace_remote([], meta=meta)
                else:
                    index.set_meta(META_ACCOUNT, account)
            except BaseException:
                if saved:
                    if previous is None:
                        paths.config_file.unlink()
                    else:
                        save_config(previous, paths.config_file)
                raise


def install(
    paths: Paths,
    options: InstallOptions,
    *,
    t: Translator,
    console: Console,
    preview: Callable[[], int],
) -> int:
    """Configure a mirror and optionally install units. Raise localized configuration errors."""
    if not options.unattended and not sys.stdin.isatty():
        raise ConfigError(code="wizard.interactive_required")
    context.validate_state_paths(paths)
    previous = _existing_config(paths)
    replace_client = (
        previous is not None
        and not options.client.provided
        and not options.unattended
        and confirm(t, console, t("wizard.replace_client"))
    )
    oauth = (
        _resolve_client(options.client, t, console, unattended=options.unattended)
        if previous is None or options.client.provided or replace_client
        else previous.oauth
    )
    source = _get_source(options, previous.source if previous else None, t, console)
    target_id = _get_target(options, previous.target if previous else None, t, console)
    daily_at = _get_time(options, previous, t, console)
    target = (
        previous.target
        if previous and previous.target.folder_id == target_id
        else TargetConfig(target_id)
    )
    cfg = replace(
        previous or Config(source, target, oauth, exclude=DEFAULT_EXCLUDES),
        source=source,
        target=target,
        oauth=oauth,
        schedule=ScheduleConfig(daily_at),
    )
    install_units = _setup_service(options, previous, daily_at, t, console)
    if cfg == previous and not install_units and _setup_complete(paths, cfg):
        console.print(Text(t("wizard.already_configured")))
        return 0
    if (
        previous is not None
        and not options.assume_yes
        and (options.unattended or not confirm(t, console, t("wizard.apply")))
    ):
        console.print(Text(t("wizard.kept")))
        return 1
    executable = _service_executable() if install_units else None
    context.validate_source_scope(source, paths)
    if source.require_mount and not source.root.is_mount():
        raise NimboError(code="source_not_mounted")

    hooks = context.test_hooks(os.environ)
    store = context.secret_store(hooks, paths.config_dir)
    store.get(REFRESH_TOKEN_KEY)
    console.print(Text(t("wizard.requirements", version=".".join(map(str, sys.version_info[:3])))))
    if not shutil.which("notify-send"):
        console.print(Text(t("wizard.no_notifications"), style="dim"))

    authorize = previous is None or oauth != previous.oauth
    with context.new_http_client(loopback=hooks.api_base is not None) as http:
        drive, refresh = _drive_session(
            oauth, cfg.transfer, http, hooks, store, t, console, authorize=authorize
        )
        account = drive.about().email
        if account:
            console.print(Text(t("wizard.account", name=safe_label(account))))
        item = drive.get(target_id)
        if item is None or item.trashed or not item.is_folder:
            raise NimboError(code="target_missing")
        console.print(Text(t("wizard.target_ok", name=safe_label(item.name))))
    # A new grant is stored only after the person confirms the account that answered.
    if refresh is not None and not options.unattended and not _confirm_account(t, console, account):
        console.print(Text(t("wizard.kept")))
        return 1
    if previous is None or previous.target.folder_id != target_id:
        cfg = replace(cfg, target=TargetConfig(target_id, item.name))
    _save_setup(paths, cfg, previous, store, refresh, account)
    if executable is not None:
        service.install_units(_launcher(paths, executable), daily_at)
    console.print(Text(t("wizard.done")))
    if not options.unattended and confirm(t, console, t("wizard.preview")):
        return preview()
    return 0


def _require_recorded_account(paths: Paths, account: str | None) -> None:
    # Reconnect renews access for the mirror's account. Switching accounts goes through
    # install, which resets the journal for the new account.
    recorded = _recorded_account(paths)
    if recorded is not None and account != recorded:
        raise NimboError(code="wizard.account_mismatch")


def reconnect(
    paths: Paths, cfg: Config, options: ClientOptions, *, t: Translator, console: Console
) -> int:
    """Re-authorize Google access and optionally replace the desktop client configuration."""
    oauth = _resolve_client(options, t, console, unattended=True) if options.provided else cfg.oauth
    updated = replace(cfg, oauth=oauth)
    hooks = context.test_hooks(os.environ)
    store = context.secret_store(hooks, paths.config_dir)
    store.get(REFRESH_TOKEN_KEY)
    context.validate_state_paths(paths)
    with context.new_http_client(loopback=hooks.api_base is not None) as http:
        drive, refresh = _drive_session(
            oauth, cfg.transfer, http, hooks, store, t, console, authorize=True
        )
        account = drive.about().email
    _require_recorded_account(paths, account)
    if refresh is not None and sys.stdin.isatty():
        console.print(Text(t("wizard.account", name=safe_label(account or t("common.unknown")))))
        if not _confirm_account(t, console, account):
            console.print(Text(t("wizard.kept")))
            return 1
    with RunLock(paths.lock_file):
        if _existing_config(paths) != cfg:
            raise NimboError(code="wizard.setup_changed")
        _require_recorded_account(paths, account)
        with _store_refresh(store, refresh):
            if updated != cfg:
                save_config(updated, paths.config_file)
    console.print(Text(t("wizard.connected", name=safe_label(account or t("common.unknown")))))
    return 0


def _remove_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError as exc:
        if exc.errno not in {errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST}:
            raise


def uninstall(
    paths: Paths, *, purge: bool, assume_yes: bool, revoke: bool, t: Translator, console: Console
) -> int:
    """Remove units and optionally purge Nimbo-owned local files and OAuth credentials."""
    if purge and not assume_yes and not confirm(t, console, t("wizard.purge")):
        console.print(Text(t("wizard.kept")))
        return 0
    if purge and paths.lock_file.is_symlink():
        raise NimboError(code="cli.unsafe_path")
    directory = service.units_dir()
    if any(
        os.path.lexists(directory / unit) for unit in (service.UNIT_SERVICE, service.UNIT_TIMER)
    ):
        service.uninstall_units()
    (paths.config_dir / _LAUNCHER).unlink(missing_ok=True)
    if purge:
        with RunLock(paths.lock_file):
            hooks = context.test_hooks(os.environ)
            store = context.secret_store(hooks, paths.config_dir)
            token = store.get(REFRESH_TOKEN_KEY)
            if token and (
                revoke or (sys.stdin.isatty() and confirm(t, console, t("wizard.revoke")))
            ):
                with context.new_http_client(loopback=hooks.api_base is not None) as http:
                    oauth_base = hooks.oauth_base or hooks.api_base
                    auth.revoke(
                        token,
                        http,
                        revoke_url=f"{oauth_base}/revoke" if oauth_base else auth.REVOKE_URL,
                    )
            store.delete(REFRESH_TOKEN_KEY)
            paths.config_file.unlink(missing_ok=True)
            for path in (
                paths.db_file,
                paths.progress_file,
                paths.approval_file,
                paths.request_file,
                *(
                    paths.db_file.with_name(paths.db_file.name + suffix)
                    for suffix in context.SQLITE_SIDECARS
                ),
            ):
                path.unlink(missing_ok=True)
            if paths.log_dir.is_symlink():
                paths.log_dir.unlink()
            elif paths.log_dir.is_dir():
                for log in paths.log_dir.iterdir():
                    if _LOG_FILE.fullmatch(log.name):
                        log.unlink()
                _remove_empty(paths.log_dir)
            paths.lock_file.unlink(missing_ok=True)
            _remove_empty(paths.state_dir)
            _remove_empty(paths.config_dir)
    console.print(Text(t("wizard.purged" if purge else "wizard.uninstalled")))
    return 0

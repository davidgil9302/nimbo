"""Manage the systemd user units and desktop integration for backups."""

from __future__ import annotations

import fcntl
import os
import re
import stat
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nimbo.core.fsutil import atomic_write_text, read_json, write_json
from nimbo.core.model import RunResult, RunSummary, utc_now_iso
from nimbo.core.paths import Paths
from nimbo.core.safety import OVERWRITE_CONFIRMATION

from .i18n import Translator

UNIT_SERVICE = "nimbo-backup.service"
UNIT_TIMER = "nimbo-backup.timer"

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

_CLOCK_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
_SYSTEMD_TIME = re.compile(
    r"\S+\s+(?P<date>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)\s+(?P<tz>\S+)"
)
_SYSTEMD_ESCAPE = str.maketrans({"\\": r"\\", '"': r"\"", "\t": r"\t", "\n": r"\n", "\r": r"\r"})


@dataclass(frozen=True, slots=True)
class ServiceState:
    """Report whether both units exist, if the timer is enabled and if the service is busy."""

    installed: bool
    timer_enabled: bool
    running: bool
    next_run: datetime | None


def render_units(exe: Path, daily_at: str) -> dict[str, str]:
    """Render both user units for an executable and local 24-hour HH:MM schedule.

    Relative paths are made absolute without following symlinks. Raise ValueError
    for an invalid schedule.
    """
    if not _CLOCK_TIME.fullmatch(daily_at):
        raise ValueError("daily_at must be a 24-hour HH:MM time")
    command = str(exe.absolute()).replace("%", "%%")
    if "$" in command:
        command = ":" + command  # Disable environment expansion in argv.
    if any(char.isspace() or char in "\"'\\" for char in command):
        command = '"' + command.translate(_SYSTEMD_ESCAPE) + '"'

    return {
        UNIT_SERVICE: f"""[Unit]
Description=nimbo: back up to Google Drive
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={command} ejecutar --auto
Nice=10
IOSchedulingClass=idle
KillMode=mixed
TimeoutStopSec=120
SuccessExitStatus=3 4 5 130
""",
        UNIT_TIMER: f"""[Unit]
Description=nimbo: daily backup

[Timer]
OnCalendar=*-*-* {daily_at}:00
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
""",
    }


def units_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return the systemd user-unit directory under XDG_CONFIG_HOME or ~/.config."""
    env = os.environ if env is None else env
    home = Path(env.get("HOME") or Path.home())
    return Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "systemd" / "user"


def run_systemctl(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Call systemctl --user and capture its output, propagating launch errors and timeouts."""
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        errors="replace",
        env={**os.environ, "LC_ALL": "C"},
        timeout=30,
        check=False,
    )


def install_units(exe: Path, daily_at: str, *, run: Runner = run_systemctl) -> None:
    """Write both units and enable the timer, propagating systemctl and filesystem errors."""
    directory = units_dir()
    for name, text in render_units(exe, daily_at).items():
        atomic_write_text(directory / name, text)
    run(["daemon-reload"]).check_returncode()
    run(["enable", "--now", UNIT_TIMER]).check_returncode()


def uninstall_units(*, run: Runner = run_systemctl) -> None:
    """Disable the timer, stop the service and remove their installed files.

    Propagate systemctl and filesystem errors so uninstall can be retried.
    """
    directory = units_dir()
    timer, service = directory / UNIT_TIMER, directory / UNIT_SERVICE
    if timer.exists() or timer.is_symlink():
        run(["disable", "--now", UNIT_TIMER]).check_returncode()
    if service.exists() or service.is_symlink():
        stop_backup(run=run)
    timer.unlink(missing_ok=True)
    service.unlink(missing_ok=True)
    run(["daemon-reload"]).check_returncode()


def _unit_properties(output: str) -> dict[str, dict[str, str]]:
    units = {}
    current: dict[str, str] = {}
    for line in [*output.splitlines(), ""]:
        key, equals, value = line.strip().partition("=")
        if not key or (key == "Id" and "Id" in current):
            if "Id" in current:
                units[current["Id"]] = current
            current = {}
        if equals:
            current[key.strip()] = value.strip()
    return units


def _human_timestamp(text: str) -> datetime | None:
    match = _SYSTEMD_TIME.fullmatch(text)
    if match is None:
        return None
    clock, zone = match["date"], match["tz"]
    if zone in {"UTC", "GMT"}:
        return datetime.fromisoformat(clock).replace(tzinfo=UTC)
    if zone.startswith(("+", "-")):
        return datetime.fromisoformat(clock + zone).astimezone(UTC)

    # systemctl shows the host's local abbreviation. Match either side of a DST fold.
    local = datetime.fromisoformat(clock)
    for fold in (0, 1):
        candidate = local.replace(fold=fold).astimezone()
        if candidate.tzname() == zone:
            return candidate.astimezone(UTC)
    with suppress(ZoneInfoNotFoundError, ValueError):
        return local.replace(tzinfo=ZoneInfo(zone)).astimezone(UTC)
    return None


def _next_run(text: str) -> datetime | None:
    try:
        if text.startswith("@"):
            timestamp = float(text[1:])
            return datetime.fromtimestamp(timestamp, UTC) if timestamp else None
        return _human_timestamp(text)
    except (ValueError, OverflowError, OSError):
        return None


def service_state(*, run: Runner = run_systemctl) -> ServiceState:
    """Return the current unit state, with a UTC deadline if systemd reports one.

    Return an unavailable state when a unit is missing or systemctl cannot run.
    """
    unavailable = ServiceState(False, False, False, None)
    args = [
        UNIT_TIMER,
        UNIT_SERVICE,
        "-p",
        "Id",
        "-p",
        "ActiveState",
        "-p",
        "UnitFileState",
        "-p",
        "NextElapseUSecRealtime",
    ]
    try:
        result = run(["show", "--timestamp=unix", *args])
        if result.returncode:
            result = run(["show", *args])
    except (OSError, subprocess.SubprocessError):
        return unavailable
    if result.returncode:
        return unavailable

    units = _unit_properties(result.stdout or "")
    timer, service = units.get(UNIT_TIMER, {}), units.get(UNIT_SERVICE, {})
    timer_state, service_file = timer.get("UnitFileState", ""), service.get("UnitFileState", "")
    missing = {"", "not-found", "n/a"}
    if timer_state in missing or service_file in missing:
        return unavailable
    return ServiceState(
        installed=True,
        timer_enabled=timer_state in {"enabled", "enabled-runtime"},
        running=service.get("ActiveState") in {"activating", "active", "reloading", "deactivating"},
        next_run=_next_run(timer.get("NextElapseUSecRealtime", "")),
    )


@contextmanager
def _lock_requests(directory: Path) -> Iterator[None]:
    # Lock the directory inode so atomic replacement of request.json stays safe.
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read_request(path: Path) -> dict[str, object] | None:
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        data = read_json(path)
    except (OSError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def start_backup(paths: Paths, *, trigger: str = "manual", run: Runner = run_systemctl) -> None:
    """Record the trigger and queue a nonblocking start, propagating I/O and systemctl errors."""
    request = paths.request_file
    request.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _lock_requests(request.parent):
        previous = _read_request(request)
        write_json(request, {"trigger": trigger, "created": utc_now_iso()})
        try:
            run(["start", "--no-block", UNIT_SERVICE]).check_returncode()
        except (OSError, subprocess.SubprocessError):
            with suppress(OSError):
                if previous is None:
                    request.unlink(missing_ok=True)
                else:
                    write_json(request, previous)
            raise


def stop_backup(*, run: Runner = run_systemctl) -> None:
    """Stop the backup service, propagating systemctl errors."""
    run(["stop", UNIT_SERVICE]).check_returncode()


def take_request(
    paths: Paths, *, now: datetime, max_age: timedelta = timedelta(minutes=10)
) -> str | None:
    """Consume a request and return its trigger if it is fresh, valid and removable."""
    request = paths.request_file
    try:
        with _lock_requests(request.parent):
            data = _read_request(request)
            request.unlink()
    except OSError:
        return None
    if data is None:
        return None
    trigger, created = data.get("trigger"), data.get("created")
    if not isinstance(trigger, str) or not trigger or not isinstance(created, str):
        return None
    try:
        age = now - datetime.fromisoformat(created)
    except (ValueError, TypeError, OverflowError):
        return None
    return trigger if timedelta() <= age <= max_age else None


@contextmanager
def idle_inhibitor(why: str) -> Iterator[None]:
    """Hold a logind idle inhibitor while the body runs, ignoring integration failures."""
    descriptor = None
    # D-Bus is optional even when its client library is installed.
    with suppress(Exception):
        from jeepney import DBusAddress, new_method_call
        from jeepney.io import blocking
        from jeepney.wrappers import unwrap_msg

        manager = DBusAddress(
            "/org/freedesktop/login1",
            bus_name="org.freedesktop.login1",
            interface="org.freedesktop.login1.Manager",
        )
        message = new_method_call(manager, "Inhibit", "ssss", ("idle", "nimbo", why, "block"))
        with blocking.open_dbus_connection("SYSTEM", enable_fds=True) as connection:
            (descriptor,) = unwrap_msg(connection.send_and_get_reply(message, timeout=5))
    try:
        yield
    finally:
        if descriptor is not None:
            with suppress(Exception):
                descriptor.close()


_NOTIFICATION_ICONS = {
    RunResult.OK: "emblem-ok-symbolic",
    RunResult.PARTIAL: "dialog-warning",
    RunResult.FAILED: "dialog-error",
    RunResult.NEEDS_CONFIRMATION: "dialog-warning",
    RunResult.PAUSED: "media-playback-pause",
    RunResult.SKIPPED: "dialog-information",
}


def _send_notification(args: Sequence[str]) -> None:
    subprocess.run(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False
    )


class DesktopNotifier:
    """Send localized backup notifications to the desktop when policy calls for one."""

    def __init__(
        self, t: Translator, *, send: Callable[[Sequence[str]], None] | None = None
    ) -> None:
        """Use a translator and an optional replacement for the notify-send launcher."""
        self._t = t
        self._send = _send_notification if send is None else send

    def run_finished(self, summary: RunSummary, *, manual: bool) -> None:
        """Notify for the eligible run and ignore missing or unavailable desktop tools."""
        result = summary.result
        manual_only = result in {RunResult.OK, RunResult.SKIPPED}
        if result is RunResult.CANCELLED or (manual_only and not manual):
            return
        t = self._t
        notice = (
            OVERWRITE_CONFIRMATION
            if result is RunResult.NEEDS_CONFIRMATION and summary.reason == OVERWRITE_CONFIRMATION
            else result
        )
        title = t(f"notify.{notice}.title")
        urgent = result in {RunResult.FAILED, RunResult.NEEDS_CONFIRMATION}
        urgency = "critical" if urgent else "normal"
        args = ["notify-send", "-a", "nimbo", "-i", _NOTIFICATION_ICONS[result], "-u", urgency]
        with suppress(OSError, subprocess.SubprocessError):
            self._send([*args, title, self._body(summary)])

    def _body(self, summary: RunSummary) -> str:
        t = self._t
        if summary.result is RunResult.OK:
            return t(
                "notify.ok.body",
                uploaded=t.plural("notify.uploaded", summary.uploaded),
                updated=t.plural("notify.updated", summary.updated),
                trashed=t.plural("notify.trashed", summary.trashed),
            )
        if summary.result is RunResult.PARTIAL and summary.failed:
            return t.plural("notify.incomplete", len(summary.failed))
        reason = f"reason.{summary.reason}"
        return t(reason) if t.has(reason) else t(f"notify.{summary.result}.body")

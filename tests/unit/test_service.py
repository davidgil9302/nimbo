from __future__ import annotations

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from jeepney import HeaderFields, new_error, new_method_return
from jeepney.fds import FileDescriptor
from jeepney.io import blocking

from nimbo.app import service
from nimbo.app.i18n import Translator
from nimbo.app.service import UNIT_SERVICE, UNIT_TIMER, DesktopNotifier, ServiceState
from nimbo.core.fsutil import read_json, write_json
from nimbo.core.model import RunResult, RunSummary
from nimbo.core.paths import Paths

_NOW = datetime(2026, 11, 23, 20, 0, tzinfo=UTC)


class _Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")


def test_render_units_snapshot():
    assert service.render_units(Path("/opt/backup tools/bin/backup"), "07:30") == {
        UNIT_SERVICE: """[Unit]
Description=nimbo: back up to Google Drive
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart="/opt/backup tools/bin/backup" ejecutar --auto
Nice=10
IOSchedulingClass=idle
KillMode=mixed
TimeoutStopSec=120
SuccessExitStatus=3 4 5 130
""",
        UNIT_TIMER: """[Unit]
Description=nimbo: daily backup

[Timer]
OnCalendar=*-*-* 07:30:00
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
""",
    }


def test_render_units_escapes_systemd_syntax(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert (
        f'ExecStart="{tmp_path}/run-%%h-\\"-\\\\-\\t-\\n" ejecutar --auto\n'
        in service.render_units(Path('run-%h-"-\\-\t-\n'), "23:59")[UNIT_SERVICE]
    )
    assert (
        "ExecStart=/opt/bin/backup ejecutar --auto\n"
        in service.render_units(Path("/opt/bin/backup"), "00:00")[UNIT_SERVICE]
    )
    assert (
        'ExecStart=":/opt/$HOME/bin/backup tools" ejecutar --auto\n'
        in service.render_units(Path("/opt/$HOME/bin/backup tools"), "00:00")[UNIT_SERVICE]
    )


@pytest.mark.parametrize("daily_at", ["6:10", "24:00", "00:60", "20:00\n[Service]"])
def test_render_units_rejects_invalid_daily_time(daily_at):
    with pytest.raises(ValueError):
        service.render_units(Path("/opt/bin/backup"), daily_at)


def test_units_dir_honors_xdg_with_home_fallback(tmp_path):
    home = tmp_path / "home"
    config = tmp_path / "xdg"
    assert service.units_dir({"HOME": str(home)}) == home / ".config/systemd/user"
    assert service.units_dir({"HOME": str(home), "XDG_CONFIG_HOME": ""}) == (
        home / ".config/systemd/user"
    )
    assert service.units_dir({"HOME": str(home), "XDG_CONFIG_HOME": str(config)}) == (
        config / "systemd/user"
    )


def test_install_writes_units_and_enables_timer():
    exe = Path("/opt/bin/backup")
    run = _Runner()
    service.install_units(exe, "20:00", run=run)
    for name, content in service.render_units(exe, "20:00").items():
        assert (service.units_dir() / name).read_text(encoding="utf-8") == content
    assert run.calls == [["daemon-reload"], ["enable", "--now", UNIT_TIMER]]


def test_install_does_not_enable_after_reload_fails():
    calls = []

    def run(args):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 1, "", "Failed to reload")

    with pytest.raises(subprocess.CalledProcessError):
        service.install_units(Path("/opt/bin/backup"), "20:00", run=run)
    assert calls == [["daemon-reload"]]


def test_uninstall():
    service.install_units(Path("/opt/bin/backup"), "20:00", run=_Runner())
    extra = service.units_dir() / "unrelated.service"
    extra.touch()
    run = _Runner()
    service.uninstall_units(run=run)
    assert list(service.units_dir().iterdir()) == [extra]
    assert run.calls == [
        ["disable", "--now", UNIT_TIMER],
        ["stop", UNIT_SERVICE],
        ["daemon-reload"],
    ]
    service.uninstall_units(run=run)
    assert list(service.units_dir().iterdir()) == [extra]


def _show(next_run, *, timer_state="enabled", service_state="activating", service_file="static"):
    return (
        f"ActiveState=active\nUnitFileState={timer_state}\nId={UNIT_TIMER}\n"
        f"NextElapseUSecRealtime={next_run}\n\n"
        f"Id={UNIT_SERVICE}\nNextElapseUSecRealtime=\n"
        f"UnitFileState={service_file}\nActiveState={service_state}\n"
    )


@pytest.mark.parametrize(
    ("timestamp", "timer", "active", "expected"),
    [
        (
            "@1764000000",
            "enabled",
            "activating",
            ServiceState(True, True, True, datetime.fromtimestamp(1764000000, UTC)),
        ),
        (
            "@1764000000.5",
            "enabled-runtime",
            "deactivating",
            ServiceState(True, True, True, datetime.fromtimestamp(1764000000.5, UTC)),
        ),
        (
            "Mon 2026-11-23 20:00:00 UTC",
            "disabled",
            "inactive",
            ServiceState(True, False, False, _NOW),
        ),
        (
            "Mon 2026-11-23 14:00:00 -0600",
            "enabled",
            "active",
            ServiceState(True, True, True, _NOW),
        ),
        (
            "Mon 2026-11-23 21:00:00 Europe/Paris",
            "enabled",
            "active",
            ServiceState(True, True, True, _NOW),
        ),
        ("n/a", "disabled", "failed", ServiceState(True, False, False, None)),
        ("", "enabled", "inactive", ServiceState(True, True, False, None)),
        ("@0", "enabled", "active", ServiceState(True, True, True, None)),
        ("not a date", "enabled", "inactive", ServiceState(True, True, False, None)),
        ("@1e300", "enabled", "inactive", ServiceState(True, True, False, None)),
        (
            "Mon 2026-11-23 20:00:00 Missing/Zone",
            "enabled",
            "inactive",
            ServiceState(True, True, False, None),
        ),
    ],
)
def test_service_state_parsing(timestamp, timer, active, expected):
    def run(args):
        assert args[0] == "show" and "--timestamp=unix" in args
        return subprocess.CompletedProcess(
            args, 0, _show(timestamp, timer_state=timer, service_state=active), ""
        )

    assert service.service_state(run=run) == expected


def test_service_state_retries_without_timestamp_option():
    calls = []

    def run(args):
        calls.append(args)
        if "--timestamp=unix" in args:
            return subprocess.CompletedProcess(args, 1, "", "Unrecognized option")
        output = (
            f"Id={UNIT_SERVICE}\nUnitFileState=static\nActiveState=active\n\n"
            + _show("Mon 2026-11-23 20:00:00 GMT").split("\n\n", 1)[0]
        )
        return subprocess.CompletedProcess(args, 0, output, "")

    assert service.service_state(run=run) == ServiceState(True, True, True, _NOW)
    assert len(calls) == 2


def test_service_state_reads_adjacent_id_first_blocks():
    output = (
        f"Id={UNIT_TIMER}\nUnitFileState=enabled\n"
        f"Id={UNIT_SERVICE}\nActiveState=active\nUnitFileState=static"
    )

    def run(args):
        return subprocess.CompletedProcess(args, 0, output, "")

    assert service.service_state(run=run) == ServiceState(True, True, True, None)


def test_service_state_resolves_local_dst_fold_in_pretty_timestamps(monkeypatch):
    def run(args):
        output = _show("Sun 2026-11-01 01:30:00 EST")
        return subprocess.CompletedProcess(args, 0, output, "")

    try:
        with monkeypatch.context() as env:
            env.setenv("TZ", "America/New_York")
            time.tzset()
            assert service.service_state(run=run).next_run == datetime(
                2026, 11, 1, 6, 30, tzinfo=UTC
            )
    finally:
        time.tzset()


def test_service_state_returns_unavailable_when_user_manager_fails():
    def run(args):
        return subprocess.CompletedProcess(args, 1, None, "Failed to connect to user bus")

    assert service.service_state(run=run) == ServiceState(False, False, False, None)


@pytest.mark.parametrize(
    "output",
    [
        "",
        _show("n/a", service_file=""),
        _show("n/a", service_file="not-found"),
        f"Id={UNIT_TIMER}\nUnitFileState=enabled",
    ],
)
def test_service_state_missing_units(output):
    def run(args):
        return subprocess.CompletedProcess(args, 0, output, "")

    assert service.service_state(run=run) == ServiceState(False, False, False, None)


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.TimeoutExpired("systemctl", 30)])
def test_service_state_without_systemctl(error):
    def run(args):
        raise error

    assert service.service_state(run=run) == ServiceState(False, False, False, None)


def test_run_systemctl_invocation(monkeypatch):
    def run(args, **kwargs):
        assert args == ["systemctl", "--user", "daemon-reload"]
        assert kwargs["capture_output"] and kwargs["text"]
        assert kwargs["timeout"] == 30
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert service.run_systemctl(["daemon-reload"]).returncode == 0


def test_start_backup_writes_request(monkeypatch):
    monkeypatch.setattr(service, "utc_now_iso", _NOW.isoformat)
    paths = Paths.default()

    def run(args):
        assert read_json(paths.request_file) == {"created": _NOW.isoformat(), "trigger": "manual"}
        assert list(args) == ["start", "--no-block", UNIT_SERVICE]
        return subprocess.CompletedProcess(args, 0, "", "")

    service.start_backup(paths, run=run)
    assert service.take_request(paths, now=_NOW) == "manual"


def test_start_backup_cleans_failed_request():
    paths = Paths.default()

    def run(args):
        return subprocess.CompletedProcess(args, 1, "", "Failed to start")

    with pytest.raises(subprocess.CalledProcessError):
        service.start_backup(paths, trigger="custom", run=run)
    assert not paths.request_file.exists()


def test_start_backup_restores_previous_request_if_queueing_fails(monkeypatch):
    monkeypatch.setattr(service, "utc_now_iso", _NOW.isoformat)
    paths = Paths.default()
    service.start_backup(paths, trigger="first", run=_Runner())

    def run(args):
        raise FileNotFoundError("systemctl")

    with pytest.raises(FileNotFoundError):
        service.start_backup(paths, trigger="second", run=run)
    assert service.take_request(paths, now=_NOW) == "first"


def test_stop_backup_stops_service():
    run = _Runner()
    service.stop_backup(run=run)
    assert run.calls == [["stop", UNIT_SERVICE]]


def test_take_request_fresh_stale_missing():
    paths = Paths.default()
    assert service.take_request(paths, now=_NOW) is None
    for age, expected in [(0, "custom"), (10, "custom"), (11, None), (-1, None)]:
        created = _NOW - timedelta(minutes=age)
        write_json(paths.request_file, {"trigger": "custom", "created": created.isoformat()})
        assert service.take_request(paths, now=_NOW) == expected
        assert not paths.request_file.exists()


def test_take_request_has_only_one_consumer(monkeypatch):
    monkeypatch.setattr(service, "utc_now_iso", _NOW.isoformat)
    paths = Paths.default()
    service.start_backup(paths, run=_Runner())
    with ThreadPoolExecutor(max_workers=8) as pool:
        readers = [pool.submit(service.take_request, paths, now=_NOW) for _ in range(8)]
    assert [reader.result() for reader in readers].count("manual") == 1
    assert not paths.request_file.exists()


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"trigger": 10},
        {"trigger": "", "created": _NOW.isoformat()},
        {"trigger": "manual", "created": "invalid"},
    ],
)
def test_take_request_discards_malformed_data(value):
    paths = Paths.default()
    write_json(paths.request_file, value)
    assert service.take_request(paths, now=_NOW) is None
    assert not paths.request_file.exists()


def test_take_request_unlinks_symlinks_without_reading_the_target(tmp_path):
    paths = Paths.default()
    target = tmp_path / "unrelated.json"
    write_json(target, {"trigger": "custom", "created": _NOW.isoformat()})
    paths.request_file.parent.mkdir(parents=True)
    paths.request_file.symlink_to(target)
    assert service.take_request(paths, now=_NOW) is None
    assert not paths.request_file.is_symlink()
    assert target.is_file()


def test_idle_inhibitor_noop_without_dbus(monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("No D-Bus")

    monkeypatch.setattr(blocking, "open_dbus_connection", unavailable)
    with service.idle_inhibitor("Backing up"):
        pass
    with pytest.raises(ValueError, match="body"), service.idle_inhibitor("Backing up"):
        raise ValueError("body")


def test_idle_inhibitor_continues_if_logind_denies_request(monkeypatch):
    def reply(message, *, timeout):
        return new_error(message, "org.freedesktop.DBus.Error.AccessDenied", "s", ("Denied",))

    def open_connection(*args, **kwargs):
        return nullcontext(SimpleNamespace(send_and_get_reply=reply))

    monkeypatch.setattr(blocking, "open_dbus_connection", open_connection)
    with service.idle_inhibitor("Backing up"):
        pass


def test_idle_inhibitor_holds_and_closes_fd(monkeypatch):
    fd, writer = os.pipe()
    os.close(writer)
    descriptor = FileDescriptor(fd)

    def reply(message, *, timeout):
        assert timeout > 0
        assert message.header.fields[HeaderFields.path] == "/org/freedesktop/login1"
        assert message.header.fields[HeaderFields.destination] == "org.freedesktop.login1"
        assert message.header.fields[HeaderFields.interface] == "org.freedesktop.login1.Manager"
        assert message.header.fields[HeaderFields.member] == "Inhibit"
        assert message.header.fields[HeaderFields.signature] == "ssss"
        assert message.body == ("idle", "nimbo", "Backing up", "block")
        return new_method_return(message, "h", (descriptor,))

    def open_connection(bus, *, enable_fds):
        assert bus == "SYSTEM" and enable_fds
        return nullcontext(SimpleNamespace(send_and_get_reply=reply))

    monkeypatch.setattr(blocking, "open_dbus_connection", open_connection)
    with pytest.raises(ValueError, match="body"), service.idle_inhibitor("Backing up"):
        os.fstat(fd)
        raise ValueError("body")
    with pytest.raises(OSError):
        os.fstat(fd)


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("result", list(RunResult))
def test_notifier_policy_matrix(result, manual):
    calls = []
    t = Translator("es")
    notifier = DesktopNotifier(t, send=calls.append)
    summary = RunSummary(1, result, uploaded=1, updated=2, trashed=3, failed=[("x", "unreadable")])
    notifier.run_finished(summary, manual=manual)

    manual_only = result in (RunResult.OK, RunResult.SKIPPED)
    if result is RunResult.CANCELLED or (manual_only and not manual):
        assert calls == []
    else:
        [args] = calls
        urgent = result in (RunResult.FAILED, RunResult.NEEDS_CONFIRMATION)
        urgency = "critical" if urgent else "normal"
        assert list(args[:4]) == ["notify-send", "-a", "nimbo", "-i"]
        assert list(args[5:7]) == ["-u", urgency]
        assert args[-2:] and all(args[-2:])
        if result is RunResult.OK:
            for text in (
                "1 archivo subido",
                "2 archivos actualizados",
                "3 elementos en la papelera",
            ):
                assert text in args[-1]


def test_notification_uses_localized_reason_instead_of_detail():
    calls = []
    t = Translator("en")
    summary = RunSummary(1, RunResult.FAILED, reason="reauth_required", detail="private detail")
    DesktopNotifier(t, send=calls.append).run_finished(summary, manual=False)
    assert calls[0][-1] == t("reason.reauth_required")


def test_notification_ignores_missing_desktop_tool(monkeypatch):
    def run(*args, **kwargs):
        raise FileNotFoundError("notify-send")

    monkeypatch.setattr(subprocess, "run", run)
    DesktopNotifier(Translator("en")).run_finished(RunSummary(1, RunResult.FAILED), manual=False)

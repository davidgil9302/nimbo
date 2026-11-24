"""Command parsing, local query contracts and process exit behavior."""

from __future__ import annotations

import json
import os
import select
import signal
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from nimbo.app import cli, context
from nimbo.app.i18n import Translator
from nimbo.app.service import ServiceState
from nimbo.core.auth import StaticTokenProvider, TokenSet
from nimbo.core.config import Config, OAuthConfig, SourceConfig, TargetConfig, save_config
from nimbo.core.errors import AuthError
from nimbo.core.index import Index
from nimbo.core.model import (
    ActiveTransfer,
    FolderStat,
    Op,
    OpKind,
    Plan,
    PlanIssue,
    PlanStats,
    Preview,
    Progress,
    RunResult,
    RunSummary,
    ScanProblem,
    UndoSummary,
)
from nimbo.core.paths import Paths
from nimbo.core.safety import evaluate_brakes, write_sentinel
from nimbo.core.secrets import REFRESH_TOKEN_KEY, MemorySecretStore
from tests.fakedrive import FakeDrive


@pytest.mark.parametrize(
    ("spanish", "english", "flags"),
    [
        ("ahora", "now", ["--aqui", "--confirmar", "--si"]),
        ("pendientes", "pending", ["--json"]),
        ("estado", "status", ["--json"]),
        ("historial", "history", ["--json", "--n", "2"]),
        ("deshacer", "undo", ["1"]),
        ("verificar", "verify", ["--reparar"]),
        ("detener", "stop", []),
        ("instalar", "install", ["--no-interactivo", "--sin-servicio", "--at", "12:15"]),
        ("conectar", "connect", ["--client-json", "file.json"]),
        ("desinstalar", "uninstall", ["--borrar-datos", "--si"]),
        ("diagnostico", "doctor", []),
        ("registro", "log", ["--n", "3"]),
        ("ejecutar", "run", ["--auto"]),
    ],
)
def test_parse_all_commands_and_aliases(spanish, english, flags):
    parser = cli.build_parser(Translator("en"))
    selected = parser.parse_args([spanish, *flags])
    assert selected.command == english
    assert vars(selected) == vars(parser.parse_args([english, *flags]))


@pytest.mark.parametrize(
    ("result", "code"),
    tuple(zip(RunResult, (0, 1, 1, 3, 4, 130, 5), strict=True)),
)
def test_exit_code_mapping(result, code):
    assert cli.exit_code(result) == code


@pytest.fixture
def setup():
    paths = Paths.default()
    paths.ensure()
    cfg = Config(
        SourceConfig(Path("/media/example"), ("Projects",)),
        TargetConfig("test-folder-id", "[Mirror]"),
        OAuthConfig("example.apps.googleusercontent.com", "hidden-value"),
    )
    save_config(cfg, paths.config_file)
    index = Index(paths.db_file)
    summary = RunSummary(index.start_run("manual", "2026-01-01"), RunResult.OK)
    index.finish_run(summary)
    index.put_folder_stats([FolderStat("Projects", files_local=2)])
    index.close()
    return paths, cfg, summary


def test_estado_json_schema(setup, capsys):
    assert cli.main(["status", "--json"]) == 0
    raw = capsys.readouterr().out
    data = json.loads(raw)
    assert set(data) == {"schema", "config", "last_run", "service", "folders"}
    assert data["schema"] == 1
    assert data["config"]["source"]["folders"] == ["Projects"]
    assert data["last_run"] == setup[2].to_dict()
    assert data["folders"]["Projects"]["files_local"] == 2
    assert "oauth" not in data["config"] and "hidden-value" not in raw


def test_pendientes_json_schema(setup, capsys, monkeypatch):
    class Engine:
        def preview(self):
            plan = Plan(
                [
                    Op(OpKind.UPLOAD, "Projects/add", "new", size=3),
                    Op(OpKind.UPDATE, "Projects/change", "changed", size=4),
                    Op(OpKind.MOVE, "Projects/new-name", "renamed", src_rel="Projects/old-name"),
                    Op(OpKind.TRASH, "Projects/remove", "deleted-locally", affected_files=1),
                ],
                PlanStats(uploads=1, upload_bytes=3, updates=1, update_bytes=4, moves=1, trashes=1),
                warnings=[PlanIssue("google-native-kept", "Projects/doc")],
            )
            return Preview(plan, [], None, evaluate_brakes(plan, setup[1].safety))

    monkeypatch.setattr(cli, "build_engine", lambda *args, **kwargs: (Engine(), lambda: None))
    assert cli.main(["pendientes", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema"] == 1
    assert data["folders"]["Projects"] == {
        "upload": [{"rel": "Projects/add", "size": 3}],
        "update": [{"rel": "Projects/change", "size": 4}],
        "move": [{"from": "Projects/old-name", "to": "Projects/new-name"}],
        "trash": [{"rel": "Projects/remove", "affected_files": 1}],
    }
    assert data["totals"]["bytes_to_send"] == 7
    assert data["warnings"][0]["code"] == "google-native-kept"


def test_historial_json(setup, capsys):
    index = Index(setup[0].db_file)
    newest = RunSummary(index.start_run("timer", "2026-01-02"), RunResult.CANCELLED)
    index.finish_run(newest)
    index.close()
    assert cli.main(["history", "--json", "--n", "1"]) == 0
    assert json.loads(capsys.readouterr().out) == {"schema": 1, "runs": [newest.to_dict()]}
    assert cli.main(["historial", "--json", "--n", "0"]) == 0
    assert json.loads(capsys.readouterr().out) == {"schema": 1, "runs": []}


def test_language_flag(capsys):
    assert cli.main(["--lang", "es", "--help"]) == 0
    assert "uso:" in capsys.readouterr().out.lower()
    assert cli.main(["--idioma", "en", "ahora", "--bogus"]) == 2
    assert "usage:" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("language", ["es", "en"])
def test_every_help_is_localized(language, capsys):
    t = Translator(language)
    assert cli.main(["--lang", language, "--help"]) == 0
    top = capsys.readouterr().out
    assert top.startswith(t("cli.parser.usage"))
    assert t("cli.description") in " ".join(top.split())
    assert all(flag in top for flag in ("--version", "-v", "--no-motion", "--lang"))
    for spanish, english in cli._COMMANDS:
        description = t(f"cli.command_help.{english}")
        assert f"{spanish} ({english})" in top
        for alias in (spanish, english):
            assert cli.main([alias, "--idioma", language, "--help"]) == 0
            output = capsys.readouterr()
            assert output.out.startswith(t("cli.parser.usage"))
            assert t("cli.parser.options") in output.out and not output.err
            assert description in " ".join(output.out.split())
            assert "cli." not in output.out
            if english in ("status", "pending", "history"):
                assert t(f"cli.json_help.{english}") in " ".join(output.out.split())


def test_version_flag(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out == f"nimbo {cli.__version__}\n"


def test_ejecutar_handles_sigterm(setup):
    code = """
from nimbo.app import cli, context
from nimbo.core.model import RunResult, RunSummary

class Engine:
    def run(self, *, trigger, stop):
        print(trigger, flush=True)
        if not stop.wait(5):
            raise RuntimeError
        return RunSummary(None, RunResult.CANCELLED, trigger=trigger)

cli.build_engine = lambda *args, **kwargs: (Engine(), lambda: None)
raise SystemExit(cli.main(["--lang", "en", "ejecutar", "--auto"]))
"""
    with subprocess.Popen(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": "src"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as worker:
        try:
            assert worker.stdout is not None
            assert select.select([worker.stdout], [], [], 5)[0]
            assert worker.stdout.readline().strip() == "timer"
            worker.send_signal(signal.SIGTERM)
            stdout, stderr = worker.communicate(timeout=10)
        finally:
            if worker.poll() is None:
                worker.kill()
        assert worker.returncode == 130, stderr
        assert stdout.count("\n") == 1 and "Stopped" in stdout


def test_ahora_falls_back_in_process_without_units(setup, monkeypatch, capsys):
    class Engine:
        def run(self, *, trigger, stop):
            assert trigger == "manual"
            assert not stop.is_set()
            return setup[2]

    monkeypatch.setattr(
        cli.service, "service_state", lambda: ServiceState(False, False, False, None)
    )
    monkeypatch.setattr(cli, "build_engine", lambda *args, **kwargs: (Engine(), lambda: None))
    assert cli.main(["--lang", "en", "ahora"]) == 0
    out = capsys.readouterr().out
    assert "running in this terminal" in out and "Completed" in out


@pytest.mark.parametrize("motion_flag", ["--sin-animaciones", "--no-motion"])
def test_no_command_dispatches_lazily(setup, monkeypatch, capsys, motion_flag):
    paths, _, _ = setup
    imports = []

    def import_dashboard(name):
        imports.append(name)

        def run_tui(passed_paths, *, reduced_motion, language, engine_factory):
            assert passed_paths == paths
            assert reduced_motion and language == "es"
            engine, close = engine_factory()
            assert engine == "engine"
            close()
            return 5

        return SimpleNamespace(run_tui=run_tui)

    monkeypatch.setattr(cli, "import_module", import_dashboard)
    for name in ("NIMBO_REDUCED_MOTION", "SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)
    assert cli.main([]) == 0
    assert "Mirror" in capsys.readouterr().out
    assert not imports

    closed = []

    def build_engine(passed_paths, cfg):
        assert passed_paths == paths and cfg.language == "es"
        return "engine", lambda: closed.append(True)

    monkeypatch.setattr(cli, "build_engine", build_engine)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    assert cli.main(["--idioma", "es", motion_flag]) == 5
    assert imports == ["nimbo.app.tui.app"] and closed == [True]


def test_installed_service_gets_request_without_building_engine(setup, monkeypatch, capsys):
    queued = []
    monkeypatch.setattr(cli.service, "service_state", lambda: ServiceState(True, True, False, None))
    monkeypatch.setattr(cli.service, "start_backup", queued.append)
    assert cli.main(["--lang", "en", "now"]) == 0
    assert queued == [setup[0]]
    assert "queued" in capsys.readouterr().out


def test_ejecutar_uses_fresh_manual_request_and_prints_one_line(setup, monkeypatch, capsys):
    paths, _, _ = setup
    locked = []
    logged = []

    def take_request(passed_paths, *, now):
        assert passed_paths == paths and now.tzinfo is not None
        return "manual"

    @contextmanager
    def inhibit(reason):
        assert reason
        locked.append("open")
        try:
            yield
        finally:
            locked.append("closed")

    class Engine:
        def run(self, *, trigger, stop):
            assert locked == ["open"] and trigger == "manual" and not stop.is_set()
            return RunSummary(None, RunResult.PAUSED)

    monkeypatch.setattr(cli.service, "idle_inhibitor", inhibit)
    monkeypatch.setattr(cli.service, "take_request", take_request)
    monkeypatch.setattr(cli, "setup_logging", lambda p, *, verbose: logged.append((p, verbose)))
    monkeypatch.setattr(cli, "build_engine", lambda *args, **kwargs: (Engine(), lambda: None))
    assert cli.main(["--lang", "en", "-v", "run", "--auto"]) == 5
    assert capsys.readouterr().out.splitlines() == [
        "Paused · uploaded 0 · updated 0 · moved 0 · trashed 0 · failed 0"
    ]
    assert locked == ["open", "closed"] and logged == [(paths, True)]


def test_ejecutar_reports_setup_error_in_one_line(capsys):
    assert cli.main(["--lang", "en", "run", "--auto"]) == 1
    output = capsys.readouterr()
    assert len(output.out.splitlines()) == 1
    assert "backup install" in output.out


@pytest.mark.parametrize(("answer", "expected"), [("\n", 4), ("s\n", 0)])
def test_trash_confirmation_defaults_to_no(setup, monkeypatch, capsys, answer, expected):
    paths, _, _ = setup
    index = Index(paths.db_file)
    decision = RunSummary(
        index.start_run("manual", "2026-01-01"),
        RunResult.NEEDS_CONFIRMATION,
        fingerprint="unit-fingerprint",
        trash_preview=["Projects/name\twith-tab\t2"],
    )
    index.finish_run(decision)
    index.close()
    queued = []

    def queue(passed_paths):
        assert (
            json.loads(passed_paths.approval_file.read_text())["fingerprint"]
            == decision.fingerprint
        )
        queued.append(passed_paths)

    monkeypatch.setattr(cli.sys, "stdin", StringIO(answer))
    monkeypatch.setattr(cli.service, "service_state", lambda: ServiceState(True, True, False, None))
    monkeypatch.setattr(cli.service, "start_backup", queue)
    assert cli.main(["--lang", "es", "ahora", "--confirmar"]) == expected
    output = capsys.readouterr().out
    assert r"Projects/name\twith-tab" in output and "[s/N]" in output
    assert paths.approval_file.exists() == (expected == 0)
    assert queued == ([paths] if expected == 0 else [])


def _profile_store(paths, store):
    """Return a secret_store replacement that checks the caller names this profile."""

    def secret_store(hooks, config_dir):
        assert config_dir == paths.config_dir
        return store

    return secret_store


def test_uninstall_purges_only_owned_files_after_confirmation(setup, tmp_path, monkeypatch):
    paths, _, _ = setup
    root = tmp_path / "source"
    root.mkdir()
    write_sentinel(root, "test-root")
    (paths.log_dir / "nimbo-2026-01-01.jsonl").touch()
    unrelated = paths.config_dir / "other-app.txt"
    unrelated.touch()
    monkeypatch.setattr(cli.sys, "stdin", StringIO("\n"))
    monkeypatch.setattr(cli.service, "units_dir", lambda: tmp_path / "units")
    secrets = MemorySecretStore()
    secrets.set(REFRESH_TOKEN_KEY, "test-refresh-value", label="unit")
    monkeypatch.setattr(context, "secret_store", _profile_store(paths, secrets))
    assert cli.main(["uninstall", "--purge"]) == 0
    assert paths.db_file.exists() and secrets.get(REFRESH_TOKEN_KEY)
    assert cli.main(["uninstall", "--purge", "--yes"]) == 0
    assert not paths.state_dir.exists() and not paths.config_file.exists()
    assert secrets.get(REFRESH_TOKEN_KEY) is None
    assert unrelated.is_file() and (root / ".nimbo-root").exists()


def test_uninstall_can_revoke_before_purging(setup, tmp_path, monkeypatch):
    paths, _, _ = setup
    secrets = MemorySecretStore()
    secrets.set(REFRESH_TOKEN_KEY, "test-refresh-value", label="unit")
    contacted = []

    def revoke(token, http, *, revoke_url):
        assert paths.config_file.exists()
        contacted.append((token, revoke_url))

    monkeypatch.setenv("NIMBO_API_BASE", "http://localhost:18080")
    monkeypatch.setenv("NIMBO_OAUTH_BASE", "http://localhost:18081")
    monkeypatch.setattr(context, "secret_store", _profile_store(paths, secrets))
    monkeypatch.setattr(cli.wizard.auth, "revoke", revoke)
    monkeypatch.setattr(cli.service, "units_dir", lambda: tmp_path / "units")
    assert cli.main(["uninstall", "--purge", "--yes", "--revoke"]) == 0
    assert contacted == [("test-refresh-value", "http://localhost:18081/revoke")]
    assert secrets.get(REFRESH_TOKEN_KEY) is None


def test_writable_state_symlink_is_rejected(setup, tmp_path, capsys):
    paths, _, _ = setup
    outside = tmp_path / "unrelated"
    outside.write_text("keep", encoding="utf-8")
    paths.lock_file.symlink_to(outside)
    assert cli.main(["--lang", "en", "now", "--here"]) == 1
    assert "symbolic link" in " ".join(capsys.readouterr().err.split())
    assert outside.read_text(encoding="utf-8") == "keep"


def test_invalid_arguments_do_not_echo_credentials(capsys):
    assert cli.main(["--lang", "en", "install", "--client-secert", "test-hidden-value"]) == 2
    assert "test-hidden-value" not in capsys.readouterr().err


def test_live_view_limits_active_paths_and_escapes_terminal_controls(setup):
    _, cfg, _ = setup
    state = Progress(
        1,
        os.getpid(),
        "transfer",
        "",
        "",
        bytes_total=100,
        bytes_done=50,
        speed_bps=15,
        eta_s=4,
        active=[ActiveTransfer(f"Projects/{i}\x1b", 1, 10) for i in range(5)],
    )
    view = cli._LiveProgress(cfg, Translator("en"), reduced_motion=True)
    output = StringIO()
    Console(file=output, width=100, color_system=None).print(view.render(state, stopping=False))
    text = output.getvalue()
    assert "15 B/s" in text and "4 s" in text and "[Mirror]" in text
    assert r"Projects/3\x1b" in text and "Projects/4" not in text
    assert "\x1b" not in text


def test_systemd_install_doctor_and_uninstall_use_default_xdg(tmp_path, monkeypatch, capsys):
    xdg_config, xdg_state = tmp_path / "xdg-config", tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))
    monkeypatch.delenv("NIMBO_CONFIG_DIR")
    monkeypatch.delenv("NIMBO_STATE_DIR")
    paths = Paths.default()
    assert paths == Paths(xdg_config / "nimbo", xdg_state / "nimbo")
    service_dir = xdg_config / "systemd" / "user"
    unit_service = service_dir / cli.service.UNIT_SERVICE
    unit_timer = service_dir / cli.service.UNIT_TIMER
    (tmp_path / "source" / "Projects").mkdir(parents=True)
    (tmp_path / "source" / "Projects" / "note.txt").touch()
    fake = FakeDrive()
    target = fake.add_folder("Team mirror")
    fake.oauth.add_access_token("test-access-value")
    secrets = MemorySecretStore()
    grant = TokenSet("test-access-value", "test-refresh-value", 60, "")
    authorized = []
    calls = []
    enabled = False
    next_run = datetime(2030, 1, 2, 12, 15, tzinfo=UTC)

    def authorize(oauth, http):
        assert oauth.client_id == "fixture.apps.googleusercontent.com"
        authorized.append(True)
        return grant

    def run_systemctl(command, **kwargs):
        nonlocal enabled
        assert command[:2] == ["systemctl", "--user"]
        args = command[2:]
        calls.append(tuple(args))
        output = ""
        if args[0] == "show":
            output = (
                f"Id={unit_timer.name}\n"
                f"UnitFileState={'enabled' if enabled else 'not-found'}\n"
                f"NextElapseUSecRealtime=@{int(next_run.timestamp()) if enabled else 0}\n\n"
                f"Id={unit_service.name}\n"
                f"UnitFileState={'static' if unit_service.is_file() else 'not-found'}\n"
                "ActiveState=inactive\n"
            )
        elif args == ["enable", "--now", unit_timer.name]:
            assert unit_service.is_file() and unit_timer.is_file()
            enabled = True
        elif args == ["disable", "--now", unit_timer.name]:
            enabled = False
        else:
            assert args in (["show-environment"], ["daemon-reload"], ["stop", unit_service.name])
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.delenv("NIMBO_OAUTH_BASE", raising=False)
    monkeypatch.setattr(
        context,
        "secret_store",
        lambda hooks, config_dir: (
            secrets if hooks.api_base is None and config_dir == paths.config_dir else None
        ),
    )
    monkeypatch.setattr(context, "new_http_client", lambda **kw: fake.client())
    monkeypatch.setattr(cli.wizard.auth, "authorize_interactive", authorize)
    monkeypatch.setattr(cli.wizard, "_service_executable", lambda: tmp_path / "backup")
    monkeypatch.setattr(cli.service.subprocess, "run", run_systemctl)
    args = [
        "--lang",
        "en",
        "install",
        "--non-interactive",
        "--client-id",
        "fixture.apps.googleusercontent.com",
        "--client-secret",
        "test-secret-value",
        "--source",
        str(tmp_path / "source"),
        "--folders",
        "Projects",
        "--target",
        target,
        "--at",
        "12:15",
    ]
    assert cli.main(args) == 0
    output = capsys.readouterr()
    assert all(
        value not in output.out + output.err
        for value in (
            "test-access-value",
            "test-refresh-value",
            "test-secret-value",
        )
    )
    assert authorized == [True] and secrets.get(REFRESH_TOKEN_KEY) == "test-refresh-value"
    launcher = paths.config_dir / "systemd-launch.sh"
    assert launcher.stat().st_mode & 0o777 == 0o700
    assert "OnCalendar=*-*-* 12:15:00" in unit_timer.read_text(encoding="utf-8")
    service_text = unit_service.read_text(encoding="utf-8")
    assert str(launcher) in service_text and "ejecutar --auto" in service_text
    assert calls[1:] == [
        ("show-environment",),
        ("daemon-reload",),
        ("enable", "--now", unit_timer.name),
    ]

    def unexpected(*args, **kwargs):
        pytest.fail("Repeating the same installation prompted or reopened credentials")

    files = (paths.config_file, paths.db_file, unit_service, unit_timer, launcher)
    installed = [(path.read_bytes(), path.stat().st_mtime_ns) for path in files]
    with monkeypatch.context() as repeat:
        repeat.setattr(cli.sys.stdin, "isatty", lambda: True)
        repeat.setattr(cli.wizard, "confirm", unexpected)
        repeat.setattr(context, "secret_store", unexpected)
        repeat.setattr(context, "new_http_client", unexpected)
        calls_before = len(calls)
        assert cli.main(args) == 0
        assert cli.main([arg for arg in args if arg != "--non-interactive"]) == 0
        assert "already configured" in capsys.readouterr().out
        assert [call[0] for call in calls[calls_before:]] == ["show", "show"]
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in files] == installed

    monkeypatch.setattr(
        context, "token_provider", lambda *args: StaticTokenProvider("test-access-value")
    )
    assert cli.main(["--lang", "en", "doctor"]) == 0
    checks = capsys.readouterr().out
    assert "✓ The systemd units are installed and the timer is enabled." in checks
    assert (
        f"✓ Next scheduled backup · {next_run.astimezone().isoformat(timespec='minutes')}" in checks
    )

    other_unit = service_dir / "other.service"
    other_unit.touch()
    assert cli.main(["desinstalar"]) == 0
    assert calls[-3:] == [
        ("disable", "--now", unit_timer.name),
        ("stop", unit_service.name),
        ("daemon-reload",),
    ]
    assert not enabled
    assert not any(path.exists() for path in (launcher, unit_service, unit_timer))
    assert other_unit.is_file()
    assert paths.config_file.exists() and secrets.get(REFRESH_TOKEN_KEY) == "test-refresh-value"
    assert cli.main(["--lang", "en", "doctor"]) == 0
    checks = capsys.readouterr().out
    assert "✗ The daily service is unavailable or disabled." in checks
    assert "✗ There is no next scheduled backup." in checks


@pytest.mark.parametrize("command", ["detener", "stop"])
def test_stop_dispatches_and_reports_unavailable(command, monkeypatch, capsys):
    called = []
    state = ServiceState(True, True, True, None)
    monkeypatch.setattr(cli.service, "service_state", lambda: state)
    monkeypatch.setattr(cli.service, "stop_backup", lambda: called.append(True))
    assert cli.main(["--lang", "en", command]) == 0
    assert "The service has stopped." in capsys.readouterr().out
    state = ServiceState(False, False, False, None)
    assert cli.main(["--lang", "en", command]) == 3
    assert "unavailable" in capsys.readouterr().out
    assert called == [True]


def test_registro_honors_line_limit(setup, capsys):
    paths, _, _ = setup
    records = [{"msg": f"entry {i}"} for i in range(3)]
    (paths.log_dir / "nimbo-2026-01-01.jsonl").write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )
    assert cli.main(["registro", "--n", "2"]) == 0
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == records[-2:]
    assert cli.main(["log", "--n", "0"]) == 0
    assert not capsys.readouterr().out


def test_deshacer_passes_explicit_run_id(setup, monkeypatch, capsys):
    class Engine:
        def undo(self, run_id, *, stop):
            assert run_id == 13 and not stop.is_set()
            return UndoSummary(run_id=run_id, untrashed=1, restored_files=1)

    monkeypatch.setattr(cli, "build_engine", lambda *args, **kwargs: (Engine(), lambda: None))
    assert cli.main(["--lang", "en", "deshacer", "13"]) == 0
    assert "files restored 1" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["instalar", "conectar"])
@pytest.mark.parametrize("form", ["web", "malformed", "invalid_client"])
def test_client_json_rejects_non_desktop_credentials_without_outputting_secret(
    command, form, setup, tmp_path, monkeypatch, capsys, caplog
):
    secret = "fixture-must-never-be-printed"
    credentials = tmp_path / "oauth client.json"
    if form == "malformed":
        credentials.write_text(f'{{"installed": {{"client_secret": "{secret}"', encoding="utf-8")
    else:
        client_type = "web" if form == "web" else "installed"
        client_id = "fixture.apps.googleusercontent.com" if form == "web" else "invalid"

        credentials.write_text(
            json.dumps({client_type: {"client_id": client_id, "client_secret": secret}}),
            encoding="utf-8",
        )

    def unexpected(*args, **kwargs):
        pytest.fail("Invalid credentials reached OAuth or Drive")

    monkeypatch.setattr(context, "new_http_client", unexpected)
    flags = ["--no-interactivo", "--sin-servicio"] if command == "instalar" else []
    assert cli.main(["--lang", "en", command, "--client-json", str(credentials), *flags]) == 1
    output = capsys.readouterr()
    assert "desktop client" in " ".join(output.err.split())
    assert secret not in output.out + output.err + caplog.text


def test_client_json_oauth_error_does_not_expose_secret(
    setup, tmp_path, monkeypatch, capsys, caplog
):
    secret = "fixture-must-never-be-printed"
    credentials = tmp_path / "oauth client.json"
    credentials.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "fixture.apps.googleusercontent.com",
                    "client_secret": secret,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(context, "secret_store", lambda _hooks, _config_dir: MemorySecretStore())
    monkeypatch.delenv("NIMBO_OAUTH_BASE", raising=False)

    def authorization_error(oauth, http):
        assert oauth.client_secret == secret
        raise AuthError(f"provider rejected {oauth.client_secret}")

    monkeypatch.setattr(cli.wizard.auth, "authorize_interactive", authorization_error)
    assert cli.main(["--lang", "en", "connect", "--client-json", str(credentials)]) == 1
    output = capsys.readouterr()
    assert "couldn't sign in" in output.err
    assert secret not in output.out + output.err + caplog.text
    assert cli.main(["status", "--json"]) == 0
    assert secret not in capsys.readouterr().out


def test_ahora_intercepts_sigint_in_live_view(setup):
    code = """
import os
import sys
from rich.console import Console

from nimbo.app import cli
from nimbo.core.model import RunResult, RunSummary

class Engine:
    def run(self, *, trigger, stop):
        assert trigger == "manual"
        os.write(sys.__stderr__.fileno(), b"ready\\n")
        if not stop.wait(5):
            raise RuntimeError("SIGINT did not request a clean stop")
        return RunSummary(None, RunResult.CANCELLED, trigger=trigger)

cli.Console = lambda **kw: Console(force_terminal=True, **kw)
cli.build_engine = lambda *args, **kwargs: (Engine(), lambda: None)
raise SystemExit(cli.main(["--lang", "en", "ahora", "--aqui"]))
"""
    with subprocess.Popen(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": "src"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as worker:
        try:
            assert worker.stderr is not None
            assert select.select([worker.stderr], [], [], 5)[0]
            assert worker.stderr.readline().strip() == "ready"
            worker.send_signal(signal.SIGINT)
            stdout, stderr = worker.communicate(timeout=10)
        finally:
            if worker.poll() is None:
                worker.kill()
        assert worker.returncode == 130, stderr
        assert "Stopped" in stdout


@pytest.mark.parametrize("command", ["estado", "historial"])
@pytest.mark.parametrize(
    "invalid",
    [
        '{"result":"ok","failed":null}',
        '{"run_id":1,"result":"ok","uploaded":"bad count"}',
        '{"run_id":1,"result":"ok","warnings":[{"code":null,"rel":"Docs/a"}]}',
        '{"run_id":1}',
    ],
)
def test_damaged_run_exits_with_localized_index_error(setup, capsys, command, invalid):
    with sqlite3.connect(setup[0].db_file) as db:
        db.execute("UPDATE run SET summary = ?", (invalid,))

    assert cli.main(["--lang", "es", command, "--json"]) == 1

    output = capsys.readouterr()
    assert "índice local" in " ".join(output.err.split())
    assert "Traceback" not in output.err


@pytest.mark.parametrize(
    ("command", "handler"), [("instalar", "install"), ("conectar", "reconnect")]
)
@pytest.mark.parametrize("language", ["en", "es"])
def test_client_secret_option_warns_and_stays_supported(
    setup, capsys, monkeypatch, command, handler, language
):
    received = []
    monkeypatch.setattr(cli.wizard, handler, lambda *args, **kwargs: received.append(args) or 0)
    secret = "synthetic-test-secret"
    assert cli.main(["--lang", language, command, f"--client-secret={secret}"]) == 0
    client = received[0][-1]
    if handler == "install":
        client = client.client
    assert client.client_secret == secret
    output = capsys.readouterr()
    assert Translator(language)("cli.client_secret_warning") == " ".join(output.err.split())
    assert secret not in output.err + output.out
    assert cli.main(["--lang", language, command, "--help"]) == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--client-json" in help_text.split("--client-secret")[-1]


def test_verificar_lists_scan_problems_and_exits_partial(setup, capsys, monkeypatch):
    class Engine:
        def verify(self, *, repair, stop):
            return SimpleNamespace(
                missing_remote=[],
                extra_remote=[],
                mismatched=[],
                duplicates=[],
                repaired=None,
                scan_problems=[ScanProblem("unreadable", "Projects/blocked")],
            )

    monkeypatch.setattr(cli, "build_engine", lambda *args, **kwargs: (Engine(), lambda: None))
    assert cli.main(["--lang", "en", "verificar"]) == 1
    output = " ".join(capsys.readouterr().out.split())
    assert "Projects/blocked" in output
    assert Translator("en")("reason.unreadable") in output


def test_partial_backup_prints_scan_warnings(capsys):
    summary = RunSummary(
        None,
        RunResult.PARTIAL,
        reason="scan_incomplete",
        warnings=[PlanIssue("unreadable", "Projects/blocked")],
    )
    t = Translator("en")
    cli._print_summary(Console(), t, summary, compact=False)
    output = " ".join(capsys.readouterr().out.split())
    assert "Projects/blocked" in output
    assert t("reason.unreadable") in output


def test_diagnostico_can_explain_damaged_run(setup, capsys, monkeypatch):
    with sqlite3.connect(setup[0].db_file) as db:
        db.execute("UPDATE run SET summary = ?", ('{"run_id":1}',))
    monkeypatch.setattr(cli.doctor, "_cloud_checks", lambda paths, cfg: iter(()))
    monkeypatch.setattr(
        cli.service, "service_state", lambda: ServiceState(False, False, False, None)
    )

    assert cli.main(["--lang", "es", "diagnostico"]) == 1
    output = capsys.readouterr()
    assert Translator("es")("doctor.no_run.bad") in " ".join(output.out.split())
    assert "Traceback" not in output.err

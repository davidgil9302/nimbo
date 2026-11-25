"""Exercise the published module entry point against the FakeDrive HTTP server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
import pytest

from nimbo.app import cli, context
from nimbo.core.config import load_config, save_config
from nimbo.core.index import META_ROOT_UUID, META_TARGET_ID, Index
from nimbo.core.paths import Paths
from nimbo.core.safety import read_sentinel
from tests.fakedrive import FakeDrive

_REPO = Path(__file__).resolve().parents[2]
_TEST_ORIGIN = "http://127.0.0.1:8765"
_TEST_VALUE = "fake-bearer-value"


@dataclass
class Mirror:
    paths: Paths
    root: Path
    target: str
    fake: FakeDrive
    invoke: Callable[..., subprocess.CompletedProcess[str]]
    credentials: Path

    @property
    def folder(self) -> Path:
        return self.root / "Projects"

    def run(self, *args: str, expect: int = 0) -> str:
        result = self.invoke(*args)
        assert _TEST_VALUE not in result.stdout + result.stderr
        assert "local-client-secret" not in result.stdout + result.stderr
        assert result.returncode == expect, (args, result.stdout, result.stderr)
        return result.stdout

    def install(self) -> str:
        return self.run(
            "instalar",
            "--no-interactivo",
            "--sin-servicio",
            "--client-json",
            str(self.credentials),
            "--source",
            str(self.root),
            "--folders",
            "Projects",
            "--target",
            f"https://drive.google.com/drive/folders/{self.target}",
        )

    def remote_file(self, name: str):
        parent = self.fake.find(self.target, "Projects")[0]
        return self.fake.find(parent.id, name)[0]


@pytest.fixture(params=("transport", "subprocess"))
def mirror(request, tmp_path, monkeypatch, capsys) -> Iterator[Mirror]:
    fake = FakeDrive()
    fake.oauth.add_access_token(_TEST_VALUE)
    paths = Paths(tmp_path / "config", tmp_path / "state")
    root = tmp_path / "local folders"
    (root / "Projects").mkdir(parents=True)
    (root / "Projects" / "document.txt").write_bytes(b"before")
    credentials = tmp_path / "desktop client.json"
    credentials.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "fixture.apps.googleusercontent.com",
                    "client_secret": "local-client-secret",
                }
            }
        ),
        encoding="utf-8",
    )
    target = fake.add_folder("Backup destination")

    with ExitStack() as stack:
        base = _TEST_ORIGIN
        if request.param == "subprocess":
            try:
                base = stack.enter_context(fake.serve())
            except PermissionError as exc:
                pytest.skip(f"loopback HTTP is unavailable: {exc}")
        env = {
            "PYTHONPATH": "src",
            "NIMBO_CONFIG_DIR": str(paths.config_dir),
            "NIMBO_STATE_DIR": str(paths.state_dir),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={tmp_path / 'test-bus'}",
            "NIMBO_API_BASE": base,
            "NIMBO_TEST_TOKEN": _TEST_VALUE,
            "NIMBO_OAUTH_BASE": base,
            "LANG": "en_US.UTF-8",
            "LC_ALL": "C",
        }
        if request.param == "transport":
            monkeypatch.setattr(
                context,
                "new_http_client",
                lambda **kw: httpx.Client(
                    transport=httpx.MockTransport(lambda req: fake._handle(req, base))
                ),
            )
            for key, value in env.items():
                monkeypatch.setenv(key, value)

        def invoke(*args: str) -> subprocess.CompletedProcess[str]:
            command = ["--lang", "en", *args]
            if request.param == "subprocess":
                return subprocess.run(
                    [sys.executable, "-m", "nimbo", *command],
                    cwd=_REPO,
                    check=False,
                    capture_output=True,
                    text=True,
                    env={**os.environ, **env},
                    timeout=30,
                    stdin=subprocess.DEVNULL,
                )
            capture = capsys.readouterr()
            try:
                code = cli.main(command)
            finally:
                # Drain each invocation separately to exercise output exactly as a script sees it.
                capture = capsys.readouterr()
            return subprocess.CompletedProcess(command, code, capture.out, capture.err)

        yield Mirror(paths, root, target, fake, invoke, credentials)


def test_install_noninteractive_then_backup_then_status(mirror):
    assert "Backup destination" in mirror.install()
    uuid = read_sentinel(mirror.root)
    assert uuid
    index = Index(mirror.paths.db_file)
    try:
        assert index.get_meta(META_ROOT_UUID) == uuid
        assert index.get_meta(META_TARGET_ID) == mirror.target
    finally:
        index.close()
    sentinel = mirror.root / ".nimbo-root"
    setup_files = (mirror.paths.config_file, mirror.paths.db_file, sentinel)
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in setup_files]
    calls = list(mirror.fake.calls)
    assert "already configured" in mirror.install()
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in setup_files] == before
    assert list(mirror.fake.calls) == calls
    assert read_sentinel(mirror.root) == uuid
    assert mirror.paths.config_file.stat().st_mode & 0o777 == 0o600
    mirror.run("ahora", "--aqui")
    status = json.loads(mirror.run("estado", "--json"))
    assert status["last_run"]["result"] == "ok"
    assert status["last_run"]["uploaded"] == 1
    assert status["folders"]["Projects"]["files_local"] == 1
    assert status["config"]["target"] == {"folder_id": mirror.target, "label": "Backup destination"}


def test_pending_then_now_then_no_changes(mirror):
    mirror.install()
    preview = json.loads(mirror.run("pendientes", "--json"))
    assert preview["folders"]["Projects"]["upload"] == [{"rel": "Projects/document.txt", "size": 6}]
    assert mirror.fake.snapshot(mirror.target) == {}
    assert "running in this terminal" in mirror.run("ahora")
    assert mirror.remote_file("document.txt").content == b"before"
    (mirror.folder / "document.txt").write_bytes(b"after")
    changed = json.loads(mirror.run("pending", "--json"))
    assert changed["folders"]["Projects"]["update"] == [{"rel": "Projects/document.txt", "size": 5}]
    mirror.run("now", "--here")
    assert mirror.remote_file("document.txt").content == b"after"
    preview = json.loads(mirror.run("pendientes", "--json"))
    assert preview["totals"]["total_ops"] == 0


def _pending_deletion(mirror):
    mirror.install()
    cfg = load_config(mirror.paths.config_file)
    save_config(replace(cfg, safety=replace(cfg.safety, max_deletes=0)), mirror.paths.config_file)
    (mirror.folder / "keep.txt").write_bytes(b"keep")
    mirror.run("ahora", "--aqui")
    (mirror.folder / "document.txt").unlink()
    mirror.run("ahora", "--aqui", expect=4)
    return mirror.remote_file("document.txt").id


def test_delete_brake_confirm_flow(mirror):
    file_id = _pending_deletion(mirror)
    assert not mirror.fake.file(file_id).trashed
    last = json.loads(mirror.run("status", "--json"))["last_run"]
    assert last["result"] == "needs_confirmation"
    assert last["trash_preview"] == ["Projects/document.txt\t1"]
    output = mirror.run("ahora", "--aqui", "--confirmar", "--si")
    assert "Projects/document.txt" in output
    assert mirror.fake.file(file_id).trashed
    assert not mirror.paths.approval_file.exists()


def test_undo_restores(mirror):
    file_id = _pending_deletion(mirror)
    mirror.run("ahora", "--aqui", "--confirmar", "--si")
    assert mirror.fake.file(file_id).trashed
    mirror.run("deshacer")
    assert not mirror.fake.file(file_id).trashed
    assert (mirror.folder / "document.txt").read_bytes() == b"before"


def test_verify_repair(mirror):
    mirror.install()
    mirror.run("ahora", "--aqui")
    file_id = mirror.remote_file("document.txt").id
    mirror.fake.set_content(file_id, b"broken")
    assert "Different in Drive" in mirror.run("verificar", expect=1)
    assert "found no differences" in mirror.run("verificar", "--reparar")
    assert mirror.fake.file(file_id).content == b"before"


def test_doctor_reports(mirror):
    mirror.install()
    mirror.run("ahora", "--aqui")
    output = mirror.run("diagnostico")
    assert "Backup destination" in output
    assert "passed its integrity check" in output
    assert "Latest backup · Completed" in output
    assert "✓" in output and "✗" in output  # Scheduling was intentionally omitted.
    (mirror.root / ".nimbo-root").unlink()
    assert "marker is missing" in mirror.run("doctor", expect=1)


def test_reinstall_requires_approval_for_changes(mirror):
    mirror.install()
    alternate = mirror.fake.add_folder("Another destination")
    args = ("install", "--non-interactive", "--no-service", "--target", alternate)
    before = mirror.paths.config_file.read_bytes()
    marker = read_sentinel(mirror.root)
    mirror.run(*args, expect=1)
    assert mirror.paths.config_file.read_bytes() == before
    mirror.run(*args, "--yes")
    assert load_config(mirror.paths.config_file).target.folder_id == alternate
    assert read_sentinel(mirror.root) == marker


def test_install_rejects_nonfolder_without_writing_setup(mirror):
    target = mirror.fake.add_file("Not a folder", mirror.target, b"data")
    mirror.run(
        "install",
        "--non-interactive",
        "--no-service",
        "--client-json",
        str(mirror.credentials),
        "--source",
        str(mirror.root),
        "--folders",
        "Projects",
        "--target",
        target,
        expect=1,
    )
    assert not mirror.paths.config_file.exists()
    assert not mirror.paths.db_file.exists()
    assert read_sentinel(mirror.root) is None


def test_reconnect_only_checks_the_account(mirror):
    mirror.install()
    cfg = load_config(mirror.paths.config_file)
    save_config(
        replace(cfg, target=replace(cfg.target, folder_id="missing-target")),
        mirror.paths.config_file,
    )
    mirror.credentials.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "another.apps.googleusercontent.com",
                    "client_secret": "local-client-secret",
                }
            }
        ),
        encoding="utf-8",
    )
    assert "Reconnected" in mirror.run("conectar", "--client-json", str(mirror.credentials))
    updated = load_config(mirror.paths.config_file)
    assert updated.target.folder_id == "missing-target"
    assert updated.oauth.client_id == "another.apps.googleusercontent.com"

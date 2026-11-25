"""Test-wide safety nets.

* Every test gets private config/state directories (never the user's real ones).
* Outside ``-m e2e`` no test may open a network connection except to loopback.
* Hypothesis profiles: ``dev`` (default), ``ci`` and ``deep`` (HYPOTHESIS_PROFILE=deep).
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, settings

settings.register_profile("dev", max_examples=60, deadline=None)
settings.register_profile(
    "ci", max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.register_profile(
    "deep", max_examples=5000, deadline=None, suppress_health_check=list(HealthCheck)
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


@pytest.fixture(autouse=True)
def _isolated_dirs(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path_factory.mktemp("nimbo-home")
    monkeypatch.setenv("NIMBO_CONFIG_DIR", str(base / "config"))
    monkeypatch.setenv("NIMBO_STATE_DIR", str(base / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(base / "xdg-state"))
    monkeypatch.setenv("NIMBO_REDUCED_MOTION", "1")
    monkeypatch.delenv("NIMBO_API_BASE", raising=False)
    monkeypatch.delenv("NIMBO_TEST_TOKEN", raising=False)


def _is_loopback(address: Any) -> bool:
    if isinstance(address, str):  # AF_UNIX socket path
        return True
    if not (isinstance(address, (tuple, list)) and address):
        return False
    host = str(address[0])
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _no_internet(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if request.node.get_closest_marker("e2e"):
        yield
        return
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address):
            raise RuntimeError(f"tests must not reach the network (tried {address!r})")
        return real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address):
            raise RuntimeError(f"tests must not reach the network (tried {address!r})")
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    """An empty directory to use as the backup source root."""
    root = tmp_path / "FILES"
    root.mkdir()
    return root

"""Entry point and dependency seams for the Google Drive terminal dashboard."""

from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from pathlib import Path
from threading import Event
from typing import Protocol, TypeVar

from textual.app import App
from textual.worker import Worker

from nimbo.app import service
from nimbo.app.i18n import Translator, detect_language
from nimbo.core.errors import NimboError
from nimbo.core.model import Preview, UndoSummary
from nimbo.core.paths import Paths

from .data import Snapshot, load_snapshot, local_now
from .screens.dashboard import DashboardScreen
from .screens.welcome import WelcomeScreen
from .theme import Motion, reduce_motion
from .widgets import line


class EngineLike(Protocol):
    """Operations the dashboard delegates to a short-lived, independently owned engine."""

    def preview(self, *, stop: Event | None = None) -> Preview:
        """Return the current backup plan, or raise if it cannot be obtained."""

    def undo(self, run_id: int | None = None, *, stop: Event | None = None) -> UndoSummary:
        """Restore deletions from a chosen run, reporting per-item outcomes."""


EngineFactory = Callable[[], tuple[EngineLike, Callable[[], None]]]
_T = TypeVar("_T")


class NimboApp(App[int]):
    """Present service state and dispatch user actions without blocking the terminal."""

    CSS_PATH = "styles.tcss"
    TITLE = ""
    ENABLE_COMMAND_PALETTE = False
    AUTO_FOCUS = ""

    def __init__(
        self,
        paths: Paths,
        *,
        reduced_motion: bool = False,
        language: str | None = None,
        engine_factory: EngineFactory | None = None,
        diagnostics: Callable[[], list[tuple[bool, str]]] | None = None,
        now: Callable[[], datetime] = local_now,
        service_state: Callable[[], service.ServiceState] = service.service_state,
        start_backup: Callable[[Paths], None] = service.start_backup,
        stop_backup: Callable[[], None] = service.stop_backup,
        install_units: Callable[[Path, str], None] = service.install_units,
        executable: Path | None = None,
    ) -> None:
        super().__init__(ansi_color=True)
        self.paths = paths
        self._language = language
        self.t = Translator(detect_language(language))
        self.title = self.t("tui.service")
        self.engine_factory = engine_factory
        self.diagnostics = diagnostics
        self.now = now
        self.service_state = service_state
        self.start_backup = start_backup
        self.stop_backup = stop_backup
        self.install_units = install_units
        self.executable = executable
        self.motion = Motion(self, reduced=reduce_motion(reduced_motion))
        self._io = ThreadPoolExecutor(max_workers=3, thread_name_prefix="nimbo-tui")
        self.snapshot: Snapshot | None = None
        self._poll: Worker[None] | None = None
        self._dashboard: DashboardScreen | None = None

    def get_default_screen(self) -> DashboardScreen:
        """Mount the dashboard immediately while the first snapshot loads."""
        self._dashboard = DashboardScreen()
        return self._dashboard

    def on_mount(self) -> None:
        """Read the published state now and at one-second intervals."""
        self.set_interval(1, self.refresh_snapshot, name="dashboard snapshot")
        self.refresh_snapshot()

    def on_unmount(self) -> None:
        """Release the app's worker pool after Textual cancels its workers."""
        self._io.shutdown(wait=False, cancel_futures=True)

    async def run_io(self, operation: Callable[[], _T]) -> _T:
        """Await blocking work on the dashboard's application-owned pool."""
        return await asyncio.get_running_loop().run_in_executor(self._io, operation)

    def refresh_snapshot(self) -> None:
        """Schedule a new read when there is no outstanding snapshot request."""
        if self._poll is not None and not self._poll.is_finished:
            return
        self._poll = self.run_worker(self._load_snapshot())

    async def _load_snapshot(self) -> None:
        state = await self.run_io(
            partial(load_snapshot, self.paths, service_state=self.service_state, now=self.now)
        )
        dashboard = self._dashboard
        assert dashboard is not None
        self.snapshot = state
        language = detect_language(self._language or state.language)
        if language != self.t.language:
            self.t = Translator(language)
        self.title = line(state.service) or self.t("tui.service")
        await dashboard.set_snapshot(state)
        if not state.configured and self.screen is dashboard:
            self.push_screen(WelcomeScreen(state.errors))
        elif state.configured and isinstance(self.screen, WelcomeScreen):
            self.pop_screen()

    def service_executable(self) -> Path:
        """Locate the install wizard's launcher or the installed command."""
        if self.executable is not None:
            return self.executable
        launcher = self.paths.config_dir / "systemd-launch.sh"
        if launcher.is_file():
            return launcher
        sibling = Path(sys.executable).with_name("backup")
        if sibling.is_file():
            return sibling
        found = shutil.which("backup") or shutil.which("nimbo")
        if found is not None:
            return Path(found)
        raise NimboError(code="tui.error.launcher")


def run_tui(
    paths: Paths,
    *,
    reduced_motion: bool = False,
    language: str | None = None,
    engine_factory: EngineFactory | None = None,
    diagnostics: Callable[[], list[tuple[bool, str]]] | None = None,
) -> int:
    """Run the interactive dashboard until it exits and return its CLI exit code."""
    app = NimboApp(
        paths,
        reduced_motion=reduced_motion,
        language=language,
        engine_factory=engine_factory,
        diagnostics=diagnostics,
    )
    result = app.run()
    return result if result is not None else 0

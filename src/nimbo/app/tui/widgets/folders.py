"""Folder summaries with stable identities for incremental updates."""

from rich.cells import cell_len
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from nimbo.app.i18n import Translator

from ..data import FolderView
from ..theme import ACCENT, ATTENTION, ERROR, OK, Motion
from . import line, size_text
from .pulse import PulseDot
from .smoothbar import SmoothBar


class FolderRow(Horizontal):
    """Present one folder with an independent coverage meter and state label."""

    def __init__(self, motion: Motion, folder: FolderView) -> None:
        super().__init__(classes="folder-row")
        self._dot = PulseDot(motion, folder.state, classes="folder-dot")
        self._folder_label = Static(line(folder.name), classes="folder-name", markup=False)
        self._folder_label.tooltip = line(folder.name)
        self._bar = SmoothBar(motion, classes="mini-bar")
        self._status = Static("", classes="folder-state", markup=False)

    def compose(self) -> ComposeResult:
        """Place the state marker next to the name and the state text at the right."""
        yield self._dot
        yield self._folder_label
        yield self._bar
        yield self._status

    def set_folder(self, folder: FolderView, t: Translator) -> int:
        """Refresh the row and return the state label's full width in terminal cells."""
        self._dot.state = folder.state
        color = {
            "up_to_date": OK,
            "changes": ATTENTION,
            "error": ERROR,
        }.get(folder.state, ACCENT)
        self._bar.set_coverage(folder.coverage, color=color)
        self._bar.visible = folder.coverage is not None and folder.state not in {
            "no_disk",
            "paused",
            "unknown",
            "waiting",
        }
        if folder.state == "changes" and folder.pending_files is not None:
            status = t.plural("tui.folder.changes", folder.pending_files)
            if folder.pending_bytes:
                status += t("tui.separator") + size_text(folder.pending_bytes, t)
        else:
            status = t(f"tui.folder.{folder.state}")
        self._status.update(status)
        self._status.set_class(folder.state in {"waiting", "paused", "unknown", "no_disk"}, "muted")
        return cell_len(status)

    def state_capacity(self) -> int:
        """Return the cells available for the state after reserving the minimum bar."""
        bar_styles = self._bar.styles
        minimum = bar_styles.min_width
        bar_cells = (
            int(minimum.resolve(self.content_size, self.app.viewport_size)) if minimum else 0
        )
        gap = max(self._folder_label.styles.margin.right, bar_styles.margin.left)
        bar_start = self._folder_label.region.right + gap
        return max(0, self.content_region.right - bar_start - bar_styles.margin.right - bar_cells)

    def set_state_width(self, width: int) -> None:
        """Use the same state-column width as the other folder rows."""
        self._status.styles.width = width

    def sync_motion(self) -> None:
        """Restart or stop the pulse as the dashboard resumes or suspends."""
        self._dot.sync_motion()


class FolderList(Vertical):
    """Maintain configured folder rows in configuration order."""

    def __init__(self, motion: Motion) -> None:
        super().__init__(id="folder-list")
        self._motion = motion
        self._rows: dict[str, FolderRow] = {}
        self._widest_state = 0

    async def set_folders(self, folders: tuple[FolderView, ...], t: Translator) -> None:
        """Change row identity only if the configured folder list changes."""
        if tuple(self._rows) != tuple(folder.name for folder in folders):
            await self.remove_children()
            self._rows = {folder.name: FolderRow(self._motion, folder) for folder in folders}
            if self._rows:
                await self.mount(*self._rows.values())
        self._widest_state = max(
            (self._rows[folder.name].set_folder(folder, t) for folder in folders), default=0
        )
        self.call_after_refresh(self._fit_state_column)

    def on_resize(self) -> None:
        """Recalculate the shared column after the list changes size."""
        self.call_after_refresh(self._fit_state_column)

    def _fit_state_column(self) -> None:
        first = next(iter(self._rows.values()), None)
        if first is not None and first.content_size.width:
            width = min(self._widest_state, first.state_capacity())
            for row in self._rows.values():
                row.set_state_width(width)

    def sync_motion(self) -> None:
        """Keep continuous motion limited to the visible dashboard."""
        for row in self._rows.values():
            row.sync_motion()

"""A filterable preview of the changes a backup would make."""

from collections import defaultdict
from threading import Event

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Static, Tree

from nimbo.core.errors import LockedError, NimboError
from nimbo.core.lock import is_locked
from nimbo.core.model import Op, OpKind, PlanIssue, Preview, ScanProblem, rel_top

from ..widgets import line, size_text
from . import Panel, error_text


class PendingScreen(Panel[None]):
    """Load the current plan on a worker and present it as an interactive tree."""

    def __init__(self) -> None:
        super().__init__()
        self._preview: Preview | None = None

    def compose(self) -> ComposeResult:
        """Show the filter above the result tree."""
        t = self.t
        self._status = Static(t("tui.loading"), id="pending-status", markup=False)
        self._issues = Static("", classes="notice", markup=False)
        self._issues.display = False
        self._filter = Input(
            placeholder=t("tui.pending.filter"), id="pending-filter", disabled=True
        )
        self._filter.cursor_blink = not self.nimbo.motion.reduced
        self._tree: Tree[None] = Tree(t("tui.action.pending"), id="pending-tree")
        self._tree.show_root = False
        self._tree.display = False
        with Vertical(classes="panel-body"):
            yield self.heading("tui.action.pending")
            yield self._status
            yield self._issues
            yield self._filter
            yield self._tree
        yield self.footer()

    def on_mount(self) -> None:
        """Start a preview unless a backup or another engine operation owns the lock."""
        app, factory = self.nimbo, self.nimbo.engine_factory
        if app.snapshot is not None and app.snapshot.running:
            self._status.update(self.t("tui.pending.locked"))
        elif factory is None:
            self._status.update(self.t("tui.pending.command"))
        else:

            def work(stop: Event) -> Preview:
                if is_locked(app.paths.lock_file) or app.service_state().running:
                    raise LockedError()
                engine, close = factory()
                try:
                    return engine.preview(stop=stop)
                finally:
                    close()

            self.start_job(work, self._show, self._failed)

    def _failed(self, exc: Exception) -> None:
        if isinstance(exc, LockedError) or (
            isinstance(exc, NimboError) and exc.code == "already_running"
        ):
            self._status.update(self.t("tui.pending.locked"))
        else:
            self._status.update(error_text(self.t, exc, "tui.error.preview"))

    def _show(self, result: Preview) -> None:
        self._preview = result
        t = self.t
        problems: list[PlanIssue | ScanProblem] = [
            *result.plan.errors,
            *result.scan_problems,
            *result.plan.warnings,
        ]
        messages = []
        for problem in problems[:3]:
            key = f"reason.{problem.code}"
            message = t(key) if t.has(key) else t("tui.pending.issue")
            if problem.rel:
                message = t("tui.pending.issue_at", path=line(problem.rel), message=message)
            messages.append(message)
        if len(problems) > 3:
            messages.append(t("tui.pending.more_issues", count=len(problems) - 3))
        self._issues.update("\n".join(messages))
        self._issues.display = bool(messages)
        self._filter.disabled = not result.plan.ops
        self._render_tree(self._filter.value)

    @on(Input.Changed, "#pending-filter")
    def filter_changes(self, event: Input.Changed) -> None:
        """Limit the tree to operations whose source or destination matches the filter."""
        self._render_tree(event.value)

    def _render_tree(self, value: str) -> None:
        if self._preview is None:
            return
        t = self.t
        search = value.strip().casefold()
        groups: dict[str, dict[OpKind, list[Op]]] = defaultdict(lambda: defaultdict(list))
        count = 0
        for op in self._preview.plan.ops:
            if search in op.rel.casefold() or (op.src_rel and search in op.src_rel.casefold()):
                groups[rel_top(op.rel)][op.kind].append(op)
                count += 1

        self._tree.clear()
        self._tree.root.expand()
        for folder in sorted(groups, key=str.casefold):
            by_kind = groups[folder]
            name = line(folder) or t("tui.pending.root")
            folder_node = self._tree.root.add(
                self._branch(name, [op for ops in by_kind.values() for op in ops]),
                expand=True,
            )
            for kind in OpKind:
                if ops := by_kind.get(kind):
                    node = folder_node.add(
                        self._branch(t(f"tui.pending.kind.{kind}"), ops), expand=True
                    )
                    for op in sorted(ops, key=lambda item: item.rel.casefold()):
                        node.add_leaf(self._operation(op))

        self._tree.display = bool(count)
        if not count:
            self._status.update(t("tui.pending.no_match" if search else "tui.pending.empty"))
        else:
            self._status.update(t.plural("tui.pending.count", count))

    def _branch(self, label: str, ops: list[Op]) -> Text:
        t = self.t
        text = Text(label, style="bold")
        extra = [t.plural("tui.pending.count", len(ops))]
        trashes = sum(op.affected_files for op in ops if op.kind is OpKind.TRASH)
        size = sum(op.size for op in ops if op.kind in {OpKind.UPLOAD, OpKind.UPDATE})
        if size:
            extra.append(size_text(size, t))
        if trashes:
            extra.append(t.plural("tui.confirm.files", trashes))
        sep = t("tui.separator")
        text.append(sep + sep.join(extra), "dim")
        return text

    def _operation(self, op: Op) -> Text:
        t = self.t
        name = line(op.rel)
        if op.kind is OpKind.MOVE and op.src_rel:
            name = t("tui.pending.move_path", source=line(op.src_rel), target=name)
        text = Text(name)
        if op.kind in {OpKind.UPLOAD, OpKind.UPDATE}:
            text.append(t("tui.separator") + size_text(op.size, t), "dim")
        elif op.kind is OpKind.TRASH:
            text.append(
                t("tui.separator") + t.plural("tui.confirm.files", op.affected_files), "dim"
            )
        return text

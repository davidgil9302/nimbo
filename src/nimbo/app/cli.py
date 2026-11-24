"""Expose Nimbo's bilingual command-line interface and stable query formats."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import closing, contextmanager, nullcontext
from dataclasses import asdict, replace
from datetime import UTC, datetime
from importlib import import_module
from itertools import chain
from pathlib import Path
from threading import Event
from typing import Any, Never, cast

from rich.console import Console, Group
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from nimbo import __version__
from nimbo.core import safety
from nimbo.core.config import Config, load_config
from nimbo.core.engine import Engine
from nimbo.core.errors import ConfigError, LockedError, NimboError, Stopped
from nimbo.core.index import Index
from nimbo.core.lock import RunLock
from nimbo.core.logs import read_log_tail, setup_logging
from nimbo.core.model import (
    OpKind,
    PlanIssue,
    Preview,
    Progress,
    RunResult,
    RunSummary,
    ScanProblem,
    rel_top,
)
from nimbo.core.paths import Paths
from nimbo.core.progress import read_progress

from . import doctor, service, wizard
from .context import build_engine, safe_label, validate_state_paths
from .fmt import human_duration, human_size
from .i18n import Translator, detect_language
from .run_history import read_run_summaries

_SCHEMA = 1
_EXIT_CODES = {
    RunResult.OK: 0,
    RunResult.PARTIAL: 1,
    RunResult.FAILED: 1,
    RunResult.SKIPPED: 3,
    RunResult.NEEDS_CONFIRMATION: 4,
    RunResult.PAUSED: 5,
    RunResult.CANCELLED: 130,
}
_COMMANDS = (
    ("ahora", "now"),
    ("pendientes", "pending"),
    ("estado", "status"),
    ("historial", "history"),
    ("deshacer", "undo"),
    ("verificar", "verify"),
    ("detener", "stop"),
    ("instalar", "install"),
    ("conectar", "connect"),
    ("desinstalar", "uninstall"),
    ("diagnostico", "doctor"),
    ("registro", "log"),
    ("ejecutar", "run"),
)


class _Parser(argparse.ArgumentParser):
    def __init__(self, *, translator: Translator, **kwargs: Any) -> None:
        super().__init__(add_help=False, **kwargs)
        self._t = translator
        self.add_argument("-h", "--help", action="help", help=translator("cli.option.help"))

    def format_help(self) -> str:
        """Localize the usage line and option headings in argparse's help."""
        return self._localize(super().format_help())

    def format_usage(self) -> str:
        """Return the localized usage line."""
        return self._localize(super().format_usage())

    def _localize(self, text: str) -> str:
        return (
            text.replace("usage:", self._t("cli.parser.usage"), 1)
            .replace("\noptions:", "\n" + self._t("cli.parser.options"), 1)
            .replace("\npositional arguments:", "\n" + self._t("cli.parser.positionals"), 1)
        )

    def error(self, message: str) -> Never:
        """Report malformed arguments without echoing input that may contain secrets."""
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog} · {self._t('cli.parser.invalid', prog=self.prog)}\n")


def _global_options(parser: argparse.ArgumentParser, t: Translator, *, subcommand: bool) -> None:
    default = argparse.SUPPRESS if subcommand else None
    help_for = (lambda _: argparse.SUPPRESS) if subcommand else t
    parser.add_argument(
        "--sin-animaciones",
        "--no-motion",
        dest="reduced_motion",
        action="store_true",
        default=default,
        help=help_for("cli.option.no_motion"),
    )
    parser.add_argument(
        "--idioma",
        "--lang",
        dest="language",
        choices=("es", "en"),
        default=default,
        help=help_for("cli.option.language"),
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", default=default, help=help_for("cli.option.verbose")
    )


def _count(text: str) -> int:
    if not text.isascii() or not text.isdecimal() or len(text) > 9:
        raise argparse.ArgumentTypeError
    return int(text)


def _run_id(text: str) -> int:
    if not text.isascii() or not text.isdecimal() or len(text) > 19:
        raise argparse.ArgumentTypeError
    value = int(text)
    if not 0 < value < (1 << 63):
        raise argparse.ArgumentTypeError
    return value


def _path(text: str) -> Path:
    if not text or "\0" in text:
        raise argparse.ArgumentTypeError
    return Path(text)


def _client_options(parser: argparse.ArgumentParser, t: Translator) -> None:
    parser.add_argument(
        "--client-json", type=_path, metavar=t("cli.metavar.path"), help=t("cli.option.client_json")
    )
    parser.add_argument("--client-id", metavar=t("cli.metavar.id"), help=t("cli.option.client_id"))
    parser.add_argument(
        "--client-secret", metavar=t("cli.metavar.secret"), help=t("cli.option.client_secret")
    )


def build_parser(t: Translator) -> argparse.ArgumentParser:
    """Create a parser that resolves Spanish and English commands to the same handlers."""
    parser = _Parser(
        translator=t, prog="backup", description=t("cli.description"), allow_abbrev=False
    )
    _global_options(parser, t, subcommand=False)
    parser.add_argument(
        "--version",
        action="version",
        version=t("cli.version_line", version=__version__),
        help=t("cli.option.version"),
    )
    commands = parser.add_subparsers(title=t("cli.commands"), metavar=t("cli.command"))
    sub = {}
    for es, en in _COMMANDS:
        help_text = t(f"cli.command_help.{en}")
        sub[en] = commands.add_parser(
            es,
            aliases=[en],
            translator=t,
            help=help_text,
            description=help_text,
            allow_abbrev=False,
        )
        sub[en].set_defaults(command=en)
        _global_options(sub[en], t, subcommand=True)
    parser.set_defaults(command=None)
    now = sub["now"]
    now.add_argument(
        "--aqui", "--here", dest="here", action="store_true", help=t("cli.option.here")
    )
    now.add_argument(
        "--confirmar",
        "--confirm",
        dest="confirm",
        action="store_true",
        help=t("cli.option.confirm"),
    )
    for name in ("now", "install", "uninstall"):
        sub[name].add_argument(
            "--si", "--yes", dest="yes", action="store_true", help=t("cli.option.yes")
        )
    for name in ("status", "pending", "history"):
        sub[name].add_argument("--json", action="store_true", help=t("cli.option.json"))
        sub[name].epilog = t(f"cli.json_help.{name}")
    for name, default in (("history", 20), ("log", 40)):
        sub[name].add_argument(
            "--n",
            type=_count,
            default=default,
            metavar=t("cli.metavar.number"),
            help=t("cli.option.count"),
        )
    sub["undo"].add_argument(
        "run_id", nargs="?", type=_run_id, metavar=t("cli.metavar.run"), help=t("cli.option.run_id")
    )
    sub["verify"].add_argument(
        "--reparar", "--repair", dest="repair", action="store_true", help=t("cli.option.repair")
    )
    sub["run"].add_argument("--auto", action="store_true", help=t("cli.option.auto"))
    _client_options(sub["install"], t)
    _client_options(sub["connect"], t)
    install = sub["install"]
    install.add_argument(
        "--source", type=_path, metavar=t("cli.metavar.path"), help=t("cli.option.source")
    )
    install.add_argument(
        "--folders", metavar=t("cli.metavar.folders"), help=t("cli.option.folders")
    )
    install.add_argument("--target", metavar=t("cli.metavar.target"), help=t("cli.option.target"))
    install.add_argument("--at", metavar=t("cli.metavar.time"), help=t("cli.option.at"))
    install.add_argument(
        "--no-interactivo",
        "--non-interactive",
        dest="unattended",
        action="store_true",
        help=t("cli.option.unattended"),
    )
    install.add_argument(
        "--sin-servicio",
        "--no-service",
        dest="no_service",
        action="store_true",
        help=t("cli.option.no_service"),
    )
    sub["uninstall"].add_argument(
        "--borrar-datos", "--purge", dest="purge", action="store_true", help=t("cli.option.purge")
    )
    sub["uninstall"].add_argument(
        "--revocar", "--revoke", dest="revoke", action="store_true", help=t("cli.option.revoke")
    )
    return parser


def exit_code(result: RunResult) -> int:
    """Translate an engine result to the documented shell exit status."""
    return _EXIT_CODES[result]


def _argv_language(argv: Sequence[str]) -> str | None:
    chosen = None
    for i, arg in enumerate(argv):
        if arg == "--":
            break
        name, sep, value = arg.partition("=")
        if name in {"--lang", "--idioma"}:
            if not sep and i + 1 < len(argv):
                value = argv[i + 1]
            if value in {"es", "en"}:
                chosen = value
    return chosen


def _reason(t: Translator, code: str) -> str:
    if code and t.has(f"reason.{code}"):
        return t(f"reason.{code}")
    if code and t.has(code):
        return t(code)
    return t("reason.error")


def _json(data: dict[str, Any]) -> None:
    print(json.dumps({"schema": _SCHEMA, **data}, ensure_ascii=True, allow_nan=False))


def _result_label(t: Translator, summary: RunSummary) -> str:
    return t(f"result.{summary.result}")


def _last_run(paths: Paths) -> RunSummary | None:
    if not paths.db_file.exists():
        return None
    with closing(Index.open_readonly(paths.db_file)) as index:
        runs = read_run_summaries(index, limit=1)
        return runs[0] if runs else None


def _status(
    paths: Paths, cfg: Config | None, t: Translator, console: Console, *, json_output: bool
) -> int:
    last, stats = None, {}
    if paths.db_file.exists():
        with closing(Index.open_readonly(paths.db_file)) as index:
            runs = read_run_summaries(index, limit=1)
            last, stats = (runs[0] if runs else None), index.folder_stats()
    units = service.service_state()
    if json_output:
        config = (
            None
            if cfg is None
            else {
                "source": {**asdict(cfg.source), "root": str(cfg.source.root)},
                "target": asdict(cfg.target),
                "schedule": asdict(cfg.schedule),
                "service": cfg.service,
                "language": cfg.language,
            }
        )
        _json(
            {
                "config": config,
                "last_run": last.to_dict() if last else None,
                "service": {
                    **asdict(units),
                    "next_run": units.next_run.isoformat() if units.next_run else None,
                },
                "folders": {
                    name: asdict(item)
                    for name, item in sorted(stats.items())
                    if cfg is None or name in cfg.source.folders
                },
            }
        )
        return 0
    if cfg is None:
        console.print(Text(t("cli.not_installed")))
        return 0
    console.print(
        Text(
            f"{safe_label(cfg.service)} · {safe_label(cfg.target.label or cfg.target.folder_id)}",
            style="bold",
        )
    )
    console.print(Text(t("cli.status.source", path=safe_label(cfg.source.root))))
    console.print(
        Text(t("cli.status.last_run", result=_result_label(t, last) if last else t("cli.no_runs")))
    )
    state = "running" if units.running else "enabled" if units.timer_enabled else "inactive"
    console.print(Text(t("cli.status.service", state=t(f"cli.status.{state}"))))
    if units.next_run:
        console.print(
            Text(
                t(
                    "cli.status.next_run",
                    time=units.next_run.astimezone().isoformat(timespec="minutes"),
                )
            )
        )
    table = Table(show_edge=False)
    for key in ("folder", "local_files", "pending_files"):
        table.add_column(t(f"cli.label.{key}"))
    for name in cfg.source.folders:
        item = stats.get(name)
        table.add_row(
            Text(safe_label(name)),
            str(item.files_local) if item else t("common.unknown"),
            str(item.files_pending) if item else t("common.unknown"),
        )
    console.print(table)
    return 0


def _pending_json(cfg: Config, preview: Preview) -> dict[str, Any]:
    folders: dict[str, dict[str, list[dict[str, object]]]] = {
        name: {"upload": [], "update": [], "move": [], "trash": []} for name in cfg.active_folders
    }
    plan = preview.plan
    for op in sorted(
        plan.ops, key=lambda op: (op.rel, op.kind, op.src_rel or "", op.file_id or "")
    ):
        if op.kind not in (OpKind.UPLOAD, OpKind.UPDATE, OpKind.MOVE, OpKind.TRASH):
            continue
        entries = folders.setdefault(
            rel_top(op.rel), {"upload": [], "update": [], "move": [], "trash": []}
        )
        item: dict[str, object]
        if op.kind is OpKind.MOVE:
            item = {"from": op.src_rel, "to": op.rel}
        elif op.kind is OpKind.TRASH:
            item = {"rel": op.rel, "affected_files": op.affected_files}
        else:
            item = {"rel": op.rel, "size": op.size}
        entries[op.kind].append(item)
    return {
        "folders": folders,
        "totals": {
            **asdict(plan.stats),
            "bytes_to_send": plan.stats.bytes_to_send,
            "total_ops": plan.stats.total_ops,
        },
        "warnings": [asdict(issue) for issue in plan.warnings],
        "errors": [asdict(issue) for issue in plan.errors],
        "scan_problems": [asdict(issue) for issue in preview.scan_problems],
        "brake": {"tripped": preview.brake.tripped, "reasons": preview.brake.reasons},
    }


def _pending(
    paths: Paths, cfg: Config, t: Translator, console: Console, *, json_output: bool
) -> int:
    engine, close = build_engine(paths, cfg)
    with _close_after(close):
        preview = engine.preview()
    plan = preview.plan
    if json_output:
        _json(_pending_json(cfg, preview))
    else:
        if plan.ops:
            table = Table(show_edge=False)
            table.add_column(t("cli.label.operation"))
            table.add_column(t("cli.label.path"))
            table.add_column(t("cli.label.size"), justify="right")
            for op in plan.ops:
                path = op.rel if op.src_rel is None else f"{op.src_rel} → {op.rel}"
                value = (
                    human_size(op.size, t.language)
                    if op.kind in {OpKind.UPLOAD, OpKind.UPDATE}
                    else ""
                )
                table.add_row(t(f"cli.op.{op.kind}"), Text(safe_label(path)), value)
            console.print(table)
        elif not plan.errors:
            console.print(Text(t("cli.no_changes")))
        if preview.brake.tripped:
            console.print(Text(t("reason.needs_confirmation"), style="yellow"))
        issues: Iterator[PlanIssue | ScanProblem] = chain(
            plan.errors, plan.warnings, preview.scan_problems
        )
        for issue in issues:
            console.print(
                Text(
                    f"{safe_label(issue.rel)} · {_reason(t, issue.code)}"
                    if issue.rel
                    else _reason(t, issue.code)
                )
            )
    return 1 if plan.errors else 0


def _history(
    paths: Paths, t: Translator, console: Console, *, count: int, json_output: bool
) -> int:
    runs = []
    if paths.db_file.exists():
        with closing(Index.open_readonly(paths.db_file)) as index:
            runs = read_run_summaries(index, limit=count)
    if json_output:
        _json({"runs": [run.to_dict() for run in runs]})
    elif runs:
        table = Table(show_edge=False)
        for key in ("run", "time", "result", "uploaded", "updated", "trashed"):
            table.add_column(t(f"cli.label.{key}"))
        for run in runs:
            table.add_row(
                str(run.run_id),
                Text(safe_label(run.finished_at)),
                _result_label(t, run),
                str(run.uploaded),
                str(run.updated),
                str(run.trashed),
            )
        console.print(table)
    else:
        console.print(Text(t("cli.no_runs")))
    return 0


@contextmanager
def _close_after(close: Callable[[], None]) -> Iterator[None]:
    try:
        yield
    finally:
        close()


@contextmanager
def _stop_on_signal() -> Iterator[Event]:
    stop = Event()

    def request_stop(signum: int, frame: object) -> None:
        stop.set()

    numbers = (signal.SIGINT, signal.SIGTERM)
    previous = {number: signal.signal(number, request_stop) for number in numbers}
    try:
        yield stop
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


class _LiveProgress:
    def __init__(self, cfg: Config, t: Translator, *, reduced_motion: bool) -> None:
        self._header = Text(
            f"{safe_label(cfg.service)} · {safe_label(cfg.target.label or cfg.target.folder_id)}",
            style="bold",
            no_wrap=True,
            overflow="ellipsis",
        )
        self._t = t
        self._motion = not reduced_motion
        self._fills: dict[str, float] = {}
        self._phase: tuple[int, str] | None = None

    def _bar(self, key: str, done: int, total: int) -> ProgressBar:
        fraction = min(1.0, done / total) if total else 0.0
        prior = self._fills.get(key, 0)
        value = prior + (fraction - prior) * 0.4 if self._motion and fraction > prior else fraction
        if fraction - value < 0.002:
            value = fraction
        self._fills[key] = value
        return ProgressBar(total=1.0, completed=value, pulse=self._motion and not total)

    def render(self, progress: Progress | None, *, stopping: bool) -> Group:
        """Build a current progress view, easing bars between file snapshots."""
        t = self._t
        if progress is None or progress.pid != os.getpid():
            line = t("cli.stopping" if stopping else "phase.preflight")
            return Group(self._header, Text(line), self._bar("", 0, 0))
        phase = (progress.run_id, progress.phase)
        if phase != self._phase:
            self._fills.clear()
            self._phase = phase
        rate = human_size(int(progress.speed_bps), t.language)
        eta = human_duration(progress.eta_s, t.language)
        phase_text = t("cli.stopping") if stopping else t(f"phase.{progress.phase}")
        line = t("cli.live_metrics", phase=phase_text, rate=rate, eta=eta)
        done = progress.bytes_done if progress.bytes_total else progress.ops_done
        total = progress.bytes_total or progress.ops_total
        files = progress.active[:4]
        self._fills = {
            key: fill
            for key, fill in self._fills.items()
            if not key or any(item.rel == key for item in files)
        }
        items = Table.grid(expand=True)
        items.add_column(ratio=2)
        items.add_column(ratio=1)
        for item in files:
            items.add_row(
                Text(safe_label(item.rel), overflow="ellipsis", no_wrap=True),
                self._bar(item.rel, item.done, item.total),
            )
        return Group(self._header, Text(line), self._bar("", done, total), items)


def _print_summary(console: Console, t: Translator, summary: RunSummary, *, compact: bool) -> None:
    result = _result_label(t, summary)
    reason = _reason(t, summary.reason) if summary.reason else ""
    if compact:
        line = t(
            "cli.run_line",
            result=result,
            uploaded=summary.uploaded,
            updated=summary.updated,
            moved=summary.moved,
            trashed=summary.trashed,
            failed=len(summary.failed),
        )
        # A service run emits exactly one physical line even on a narrow terminal.
        print(f"{line} · {reason}" if reason else line)
        return
    console.print(Text(result, style="bold"))
    if reason:
        console.print(Text(reason))
    table = Table(show_edge=False)
    table.add_column(t("cli.label.operation"))
    table.add_column(t("cli.label.count"), justify="right")
    counts = {
        "uploaded": summary.uploaded,
        "updated": summary.updated,
        "moved": summary.moved,
        "trashed": summary.trashed,
        "failed": len(summary.failed),
    }
    for key, count in counts.items():
        table.add_row(t(f"cli.label.{key}"), str(count))
    console.print(table)
    for rel, code in summary.failed:
        console.print(Text(f"{safe_label(rel)} · {_reason(t, code)}"))
    for warning in summary.warnings:
        console.print(Text(f"{safe_label(warning.rel)} · {_reason(t, warning.code)}"))
    if summary.result is RunResult.NEEDS_CONFIRMATION:
        console.print(Text(t("cli.confirm_next")))


def _run_backup(
    paths: Paths,
    cfg: Config,
    t: Translator,
    console: Console,
    *,
    background: bool,
    auto: bool,
    reduced_motion: bool,
    verbose: bool,
) -> int:
    with _stop_on_signal() as stop:
        validate_state_paths(paths)
        setup_logging(paths, verbose=verbose)
        trigger = "manual"
        if auto:
            request = service.take_request(paths, now=datetime.now(UTC))
            trigger = "manual" if request == "manual" else "timer"
        engine, close = build_engine(paths, replace(cfg, language=t.language))
        inhibitor = service.idle_inhibitor(t("cli.inhibit")) if background else nullcontext()
        with _close_after(close), inhibitor:
            if background or not console.is_terminal:
                summary = engine.run(trigger=trigger, stop=stop)
            else:
                view = _LiveProgress(cfg, t, reduced_motion=reduced_motion)
                with (
                    Live(
                        view.render(None, stopping=False),
                        console=console,
                        auto_refresh=False,
                        transient=True,
                    ) as live,
                    ThreadPoolExecutor(max_workers=1) as worker,
                ):
                    run = worker.submit(engine.run, trigger=trigger, stop=stop)
                    try:
                        while True:
                            try:
                                summary = run.result(timeout=0.1)
                                break
                            except TimeoutError:
                                if run.done():
                                    summary = run.result()
                                    break
                                progress = read_progress(paths.progress_file)
                                live.update(
                                    view.render(progress, stopping=stop.is_set()), refresh=True
                                )
                    finally:
                        stop.set()
    _print_summary(console, t, summary, compact=background)
    return exit_code(summary.result)


def _approve(paths: Paths, cfg: Config, t: Translator, console: Console, *, yes: bool) -> int:
    last = _last_run(paths)
    if last is None or last.result is not RunResult.NEEDS_CONFIRMATION or not last.fingerprint:
        console.print(Text(t("cli.no_confirmation")))
        return 3
    console.print(Text(t("reason.needs_confirmation")))
    table = Table(show_edge=False)
    table.add_column(t("cli.label.path"))
    table.add_column(t("cli.label.affected_files"), justify="right")
    entries = [entry.rsplit("\t", 1) for entry in last.trash_preview]
    if len(last.trash_preview) >= 500:
        engine, close = build_engine(paths, cfg)
        with _close_after(close):
            preview = engine.preview()
        if preview.plan.errors or preview.brake.fingerprint != last.fingerprint:
            console.print(Text(t("cli.confirmation_changed")))
            return 4
        entries = [
            [op.rel, str(op.affected_files)]
            for op in sorted(preview.plan.ops, key=lambda op: op.rel)
            if op.kind is OpKind.TRASH
        ]
    for entry in entries:
        table.add_row(
            Text(safe_label(entry[0])),
            Text(safe_label(entry[1])) if len(entry) == 2 else t("common.unknown"),
        )
    console.print(table)
    if not yes and not wizard.confirm(t, console, t("cli.confirm_trash")):
        return 4
    validate_state_paths(paths)
    with RunLock(paths.lock_file):
        if _last_run(paths) != last or load_config(paths.config_file) != cfg:
            console.print(Text(t("cli.confirmation_changed")))
            return 4
        safety.write_approval(paths.approval_file, last.fingerprint, now=datetime.now(UTC))
    return 0


def _undo(paths: Paths, cfg: Config, t: Translator, console: Console, run_id: int | None) -> int:
    engine, close = build_engine(paths, cfg)
    with _close_after(close), _stop_on_signal() as stop:
        summary = engine.undo(run_id, stop=stop)
    console.print(
        Text(
            t(
                "cli.undo_line",
                untrashed=summary.untrashed,
                restored=summary.restored_files,
                existing=len(summary.skipped_existing),
                failed=len(summary.failed),
            )
        )
    )
    for rel, code in summary.failed:
        console.print(Text(f"{safe_label(rel)} · {_reason(t, code)}"))
    for rel in summary.skipped_existing:
        console.print(Text(f"{safe_label(rel)} · {t('reason.restore_exists')}"))
    return 1 if summary.failed else 0


def _verify(paths: Paths, cfg: Config, t: Translator, console: Console, *, repair: bool) -> int:
    engine, close = build_engine(paths, replace(cfg, language=t.language))
    with _close_after(close), _stop_on_signal() as stop:
        report = engine.verify(repair=repair, stop=stop)
        # Repair can preserve protected duplicates or other out-of-policy items.
        final = (
            engine.verify(stop=stop)
            if report.repaired is not None and report.repaired.result is RunResult.OK
            else None
        )
    changes = {
        "missing": report.missing_remote,
        "extra": report.extra_remote,
        "mismatched": report.mismatched,
        "duplicates": report.duplicates,
    }
    for label, paths_with_issue in changes.items():
        for rel in paths_with_issue:
            console.print(Text(f"{t(f'cli.audit.{label}')} · {safe_label(rel)}"))
    for problem in report.scan_problems:
        console.print(Text(f"{safe_label(problem.rel)} · {_reason(t, problem.code)}"))
    if report.repaired is not None:
        _print_summary(console, t, report.repaired, compact=False)
        result = exit_code(report.repaired.result)
        if result:
            return result
        assert final is not None
        remaining = any(
            (
                final.missing_remote,
                final.extra_remote,
                final.mismatched,
                final.duplicates,
                final.scan_problems,
            )
        )
        for problem in final.scan_problems:
            console.print(Text(f"{safe_label(problem.rel)} · {_reason(t, problem.code)}"))
        console.print(Text(t("cli.audit.remaining" if remaining else "cli.audit.clean")))
        return int(remaining)
    if not any(changes.values()) and not report.scan_problems:
        console.print(Text(t("cli.audit.clean")))
        return 0
    if any(changes.values()):
        console.print(Text(t("cli.audit.repair_hint")))
    return 1


def _client_from_args(args: argparse.Namespace) -> wizard.ClientOptions:
    return wizard.ClientOptions(args.client_json, args.client_id, args.client_secret)


def _run_command(
    args: argparse.Namespace, paths: Paths, cfg: Config | None, t: Translator, console: Console
) -> int:
    command = args.command
    reduced = bool(
        args.reduced_motion
        or os.environ.get("NIMBO_REDUCED_MOTION") == "1"
        or os.environ.get("SSH_CONNECTION")
        or os.environ.get("SSH_TTY")
    )
    if command is None and sys.stdout.isatty():
        try:
            module = import_module("nimbo.app.tui.app")
        except ModuleNotFoundError as exc:
            if exc.name != "nimbo.app.tui.app":
                raise
            raise NimboError(code="cli.tui_unavailable") from None

        def dashboard_engine() -> tuple[Engine, Callable[[], None]]:
            return build_engine(paths, replace(load_config(paths.config_file), language=t.language))

        return cast(
            int,
            module.run_tui(
                paths, reduced_motion=reduced, language=t.language, engine_factory=dashboard_engine
            ),
        )
    if command in (None, "status"):
        return _status(paths, cfg, t, console, json_output=getattr(args, "json", False))
    if command == "history":
        return _history(paths, t, console, count=args.n, json_output=args.json)
    if command == "log":
        for entry in read_log_tail(paths, lines=args.n):
            print(json.dumps(entry, ensure_ascii=True))
        return 0
    if command == "doctor":
        return doctor.run_doctor(paths, t=t, console=console)
    if command == "stop":
        if not service.service_state().installed:
            console.print(Text(t("cli.no_service_to_stop")))
            return 3
        service.stop_backup()
        console.print(Text(t("cli.stop_requested")))
        return 0
    if command == "uninstall":
        return wizard.uninstall(
            paths, purge=args.purge, assume_yes=args.yes, revoke=args.revoke, t=t, console=console
        )
    if command == "install":
        options = wizard.InstallOptions(
            client=_client_from_args(args),
            source=args.source,
            folders=args.folders,
            target=args.target,
            at=args.at,
            unattended=args.unattended,
            no_service=args.no_service,
            assume_yes=args.yes,
        )

        def preview_setup() -> int:
            return _pending(paths, load_config(paths.config_file), t, console, json_output=False)

        return wizard.install(paths, options, t=t, console=console, preview=preview_setup)
    if cfg is None:
        raise NimboError(code="cli.not_installed")
    if command == "connect":
        return wizard.reconnect(paths, cfg, _client_from_args(args), t=t, console=console)
    if command == "pending":
        return _pending(paths, cfg, t, console, json_output=args.json)
    if command == "undo":
        return _undo(paths, cfg, t, console, args.run_id)
    if command == "verify":
        return _verify(paths, cfg, t, console, repair=args.repair)
    if command == "now":
        if args.confirm:
            code = _approve(paths, cfg, t, console, yes=args.yes)
            if code:
                return code
        if not args.here:
            if service.service_state().installed:
                service.start_backup(paths)
                console.print(Text(t("cli.queued")))
                return 0
            console.print(Text(t("cli.fallback")))
    return _run_backup(
        paths,
        cfg,
        t,
        console,
        background=command == "run",
        auto=command == "run" and args.auto,
        reduced_motion=reduced,
        verbose=bool(args.verbose),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command, returning its documented shell status."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    paths = Paths.default()
    cfg, invalid_config = None, False
    if os.path.lexists(paths.config_file):
        try:
            cfg = load_config(paths.config_file)
        except ConfigError:
            invalid_config = True
    language = _argv_language(arguments) or (cfg.language if cfg else None)
    t = Translator(detect_language(language))
    parser = build_parser(t)
    try:
        args = parser.parse_args(arguments)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    if (args.command == "uninstall" and args.revoke and not args.purge) or (
        args.command == "now" and args.yes and not args.confirm
    ):
        return _usage_error(parser)
    console = Console(highlight=False)
    stderr = Console(stderr=True, highlight=False)
    if getattr(args, "client_secret", None) is not None:
        stderr.print(Text(t("cli.client_secret_warning"), style="yellow"))
    try:
        dashboard = args.command is None and sys.stdout.isatty()
        if (
            invalid_config
            and not dashboard
            and args.command
            not in {
                "doctor",
                "uninstall",
                "history",
                "log",
                "stop",
            }
        ):
            raise ConfigError()
        return _run_command(args, paths, cfg, t, console)
    except (KeyboardInterrupt, Stopped):
        result, reason = RunResult.CANCELLED, "cancelled"
    except LockedError:
        result, reason = RunResult.SKIPPED, "already_running"
    except EOFError:
        result, reason = RunResult.FAILED, "wizard.input_closed"
    except NimboError as exc:
        result, reason = RunResult.FAILED, exc.code
    except subprocess.SubprocessError:
        result, reason = RunResult.FAILED, "cli.systemd_error"
    except (OSError, ValueError, sqlite3.Error, RecursionError):
        result, reason = RunResult.FAILED, "cli.local_error"
    if args.command == "run":
        _print_summary(console, t, RunSummary(None, result, reason=reason), compact=True)
    else:
        stderr.print(Text(_reason(t, reason)))
    return exit_code(result)


def _usage_error(parser: argparse.ArgumentParser) -> int:
    try:
        parser.error("")
    except SystemExit:
        return 2

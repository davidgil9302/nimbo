"""Edit backup folders, exclusions, daily time and deletion brakes."""

import re
from dataclasses import replace
from decimal import Decimal
from threading import Event

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Input, Label, Static, TextArea
from textual.worker import Worker

from nimbo.core.config import Config, load_config, save_config
from nimbo.core.errors import ConfigError, NimboError

from ..widgets import line
from . import FlatButton, Panel

_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
_INTEGER = re.compile(r"[0-9]+")


class SettingsScreen(Panel[None]):
    """Validate edited values, save atomically and update the systemd schedule."""

    def __init__(self) -> None:
        super().__init__()
        self._cfg: Config | None = None
        self._save_worker: Worker[None] | None = None

    def compose(self) -> ComposeResult:
        """Keep the save action and validation feedback in view while the form scrolls."""
        t = self.t
        self._form = Vertical(classes="settings-form")
        self._status = Static(t("tui.loading"), id="settings-status", markup=False)
        self._save = FlatButton(
            t("tui.action.save"), id="settings-save", classes="flat", disabled=True
        )
        with VerticalScroll(classes="panel-body settings-body"):
            yield self.heading("tui.action.settings")
            yield self._form
        yield self._status
        yield self.footer(self._save)

    def on_mount(self) -> None:
        """Read the current editable config on a worker."""
        path = self.nimbo.paths.config_file
        self.start_job(
            lambda _: load_config(path),
            self._show,
            lambda _: self._status.update(self.t("tui.error.config")),
        )

    def _field(self, key: str, id: str, value: str) -> Vertical:
        control = Input(value, id=id, select_on_focus=False)
        control.cursor_blink = not self.nimbo.motion.reduced
        return Vertical(
            Label(self.t(key), markup=False),
            control,
            classes="settings-field",
        )

    def _show(self, cfg: Config) -> None:
        self._cfg = cfg
        t = self.t
        ratio = Decimal(str(cfg.safety.max_delete_ratio)).scaleb(2).normalize()
        ratio_text = format(ratio, "f").replace(".", t("units.decimal_separator"))
        folders = [
            Checkbox(Text(line(name)), value=name not in cfg.source.paused, id=f"folder-{i}")
            for i, name in enumerate(cfg.source.folders)
        ]
        patterns = TextArea("\n".join(cfg.exclude), id="exclusions", soft_wrap=False)
        patterns.cursor_blink = not self.nimbo.motion.reduced
        self._form.mount(
            Static(t("tui.settings.folders"), classes="section-heading", markup=False),
            Vertical(*folders, classes="settings-folders"),
            Horizontal(
                self._field("tui.settings.time", "daily-at", cfg.schedule.daily_at),
                self._field("tui.settings.max_deletes", "max-deletes", str(cfg.safety.max_deletes)),
                self._field("tui.settings.max_ratio", "max-ratio", ratio_text),
                self._field("tui.settings.min_files", "min-files", str(cfg.safety.ratio_min_files)),
                classes="settings-fields",
            ),
            Static(t("tui.settings.exclusions"), classes="section-heading", markup=False),
            Static(t("tui.settings.patterns_hint"), classes="muted", markup=False),
            patterns,
        )
        self._status.update("")
        self._save.disabled = False

    def _integer(self, id: str, key: str) -> int:
        raw = self.query_one(id, Input).value.strip()
        if not _INTEGER.fullmatch(raw):
            raise ConfigError(code=key)
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(code=key) from None

    def _edited_config(self, cfg: Config) -> Config:
        daily = self.query_one("#daily-at", Input).value.strip()
        if not _TIME.fullmatch(daily):
            raise ConfigError(code="tui.settings.invalid_time")
        deletes = self._integer("#max-deletes", "tui.settings.invalid_deletes")
        min_files = self._integer("#min-files", "tui.settings.invalid_min_files")
        raw = self.query_one("#max-ratio", Input).value.strip().replace(",", ".")
        try:
            ratio = float(Decimal(raw).scaleb(-2))
        except (ArithmeticError, ValueError):
            ratio = 0
        if not 0 < ratio <= 1:
            raise ConfigError(code="tui.settings.invalid_ratio")
        paused = tuple(
            name
            for i, name in enumerate(cfg.source.folders)
            if not self.query_one(f"#folder-{i}", Checkbox).value
        )
        patterns = self.query_one("#exclusions", TextArea).text
        exclude = (
            cfg.exclude
            if patterns == "\n".join(cfg.exclude)
            else tuple(patterns.split("\n") if patterns else ())
        )
        candidate = replace(
            cfg,
            source=replace(cfg.source, paused=paused),
            schedule=replace(cfg.schedule, daily_at=daily),
            safety=replace(
                cfg.safety,
                max_deletes=deletes,
                max_delete_ratio=ratio,
                ratio_min_files=min_files,
            ),
            exclude=exclude,
        )
        try:
            candidate.matcher()
        except ConfigError:
            raise ConfigError(code="tui.settings.invalid_exclusions") from None
        return candidate

    def action_back(self) -> None:
        """Keep the result visible while a save is in progress."""
        if self._save_worker is None or self._save_worker.is_finished:
            super().action_back()

    @on(Button.Pressed, "#settings-save")
    def save(self) -> None:
        """Validate the form and apply it using a background worker."""
        app, cfg, t = self.nimbo, self._cfg, self.t
        if cfg is None or self._save.disabled:
            return
        try:
            changed = self._edited_config(cfg)
        except ConfigError as exc:
            self._status.update(t(exc.code))
            return
        self._save.disabled = True
        self._status.update(t("tui.settings.saving"))

        def work(_: Event) -> Config:
            path = app.paths.config_file
            if load_config(path) != cfg:
                raise NimboError(code="tui.settings.conflict")
            exe = app.service_executable()
            if changed != cfg:
                save_config(changed, path)
            try:
                app.install_units(exe, changed.schedule.daily_at)
            except Exception as exc:
                raise NimboError(code="tui.settings.units_failed") from exc
            return changed

        def finished(saved: Config) -> None:
            self._cfg = saved
            self._save.disabled = False
            self._status.update(t("tui.settings.saved"))
            app.refresh_snapshot()

        def failed(exc: Exception) -> None:
            key = exc.code if isinstance(exc, NimboError) else "tui.settings.save_failed"
            if key == "tui.settings.units_failed":
                self._cfg = changed
                app.refresh_snapshot()
            self._save.disabled = False
            self._status.update(t(key if t.has(key) else "tui.settings.save_failed"))

        self._save_worker = self.start_job(work, finished, failed)

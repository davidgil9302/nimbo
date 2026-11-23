"""Where nimbo keeps its files (XDG base directories, overridable for tests)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Paths:
    config_dir: Path
    state_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def db_file(self) -> Path:
        return self.state_dir / "nimbo.db"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "nimbo.lock"

    @property
    def progress_file(self) -> Path:
        return self.state_dir / "progress.json"

    @property
    def approval_file(self) -> Path:
        return self.state_dir / "approval.json"

    @property
    def request_file(self) -> Path:
        return self.state_dir / "request.json"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @classmethod
    def default(cls, env: Mapping[str, str] | None = None) -> Paths:
        env = os.environ if env is None else env
        home = Path(env.get("HOME") or Path.home())
        config = env.get("NIMBO_CONFIG_DIR") or str(
            Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "nimbo"
        )
        state = env.get("NIMBO_STATE_DIR") or str(
            Path(env.get("XDG_STATE_HOME") or home / ".local" / "state") / "nimbo"
        )
        return cls(config_dir=Path(config), state_dir=Path(state))

    def ensure(self) -> None:
        """Create both directories (private to the user)."""
        for directory in (self.config_dir, self.state_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)

"""Run one backup in a test subprocess using a private config and a loopback fake."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

from nimbo.core.config import load_config
from nimbo.core.model import RunResult
from nimbo.core.paths import Paths

if __name__ == "__main__":
    # Direct script execution with PYTHONPATH=src also needs the repository's test helpers.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.system.helpers import open_engine


def main() -> int:
    """Run the configured engine once and print its JSON summary for the parent test."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    args = parser.parse_args()
    base = httpx.URL(args.base_url)
    if base.scheme != "http" or base.host != "127.0.0.1" or not base.port:
        parser.error("base_url must be a loopback HTTP origin with an explicit port")
    try:
        paths = Paths(Path(os.environ["NIMBO_CONFIG_DIR"]), Path(os.environ["NIMBO_STATE_DIR"]))
    except KeyError as exc:
        parser.error(f"Set the private test directory {exc.args[0]}")

    config = load_config(paths.config_file)
    with (
        httpx.Client(base_url=base, trust_env=False, timeout=15) as client,
        open_engine(config, paths, client) as session,
    ):
        summary = session.engine.run(trigger="manual")
    print(json.dumps(summary.to_dict()))
    return 0 if summary.result is RunResult.OK else 1


if __name__ == "__main__":
    raise SystemExit(main())

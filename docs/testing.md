# Testing

[Back to the README](../README.md)

From the repository root, activate a Python 3.11 or newer virtual environment
and install the development dependencies with the same snapshot constraints
used by GitHub Actions.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]' -c .github/ci-constraints.txt
```

## Local and CI checks

```sh
ruff check src tests
ruff format --check src tests
mypy src
pytest -m 'not slow and not e2e' --cov=nimbo
```

GitHub Actions runs these checks on Ubuntu across Python 3.11 through 3.14 and
applies the coverage minimum in the [workflow](../.github/workflows/ci.yml).
Coverage includes branches. The scheduled job runs the slow offline tests
with the `ci` Hypothesis profile.

| Location | Coverage |
| --- | --- |
| [tests/unit](../tests/unit) | Individual modules, safety checks, and CLI behavior. |
| [tests/fakedrive](../tests/fakedrive) | In-memory Drive and OAuth, with mock HTTP transport, a loopback server, and fault injection. |
| [tests/property](../tests/property) | Hypothesis tests for planner and engine invariants. |
| [tests/system](../tests/system) | CLI integration, crash and recovery matrices, subprocess kills, and a seeded soak. |
| [tests/tui](../tests/tui) | Headless Textual interaction tests and SVG snapshots. |
| [tests/e2e](../tests/e2e) | Reserved for opt-in integration tests against a real Drive sandbox. |

The shared fixtures isolate local configuration and state. Outside `e2e`,
Python tests may only connect over loopback. Loopback integration tests can
skip in sandboxes that prohibit local sockets. The default Hypothesis profile
is `dev` (60 examples), with `ci` (300) and `deep` (5,000) available through
`HYPOTHESIS_PROFILE`.

## Slow tests and offline soak

```sh
HYPOTHESIS_PROFILE=ci pytest -m 'slow and not e2e'
NIMBO_SOAK=100 pytest -m slow tests/system/test_soak.py
```

`slow` selects cases such as subprocess kills and the offline soak. The soak
defaults to 20 iterations and exercises the engine with local mutations,
simulated Drive activity, and injected faults. It never needs Google
credentials.

## Dashboard snapshots

Use the versions in [.github/ci-constraints.txt](../.github/ci-constraints.txt)
for reproducible SVG rendering. To update a snapshot intentionally, run the
first command, inspect the SVG changes, and then run the second.

```sh
pytest tests/tui --snapshot-update
pytest tests/tui
```

## Real Drive tests

The `e2e` marker selects tests against Google Drive. The default pytest
selection and CI exclude them. Use a saved `backup` configuration with an
authorized refresh token in the local Secret Service. Set
`NIMBO_E2E_FOLDER_ID` to a dedicated, disposable Drive folder outside the
configured backup target. Each test creates a uniquely named child folder
and moves that folder to the trash during teardown.

```sh
NIMBO_E2E_FOLDER_ID="SANDBOX_FOLDER_ID" PYTHONPATH=src .venv/bin/python -m pytest tests/e2e -m e2e
NIMBO_E2E_FOLDER_ID="SANDBOX_FOLDER_ID" scripts/e2e_soak.sh 10
```

The [soak runner](../scripts/e2e_soak.sh) repeats the suite and prints a summary.
It reports an error if pytest collects no `e2e` tests.

- Set `NIMBO_E2E_LARGE=1` to include the interrupted 2 GiB upload. Allow 2 GiB
  of Drive quota for each run because files in the trash still count against it.
- Set `NIMBO_E2E_COMPUTERS_FOLDER_ID` to a disposable folder created under
  **Computers** by Google Drive for desktop. This folder must be inside
  `NIMBO_E2E_FOLDER_ID` or be the same folder. To test a separate Computers
  sandbox, set both variables to its ID and add `-k computers` to the pytest
  command.

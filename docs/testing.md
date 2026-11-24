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

The `e2e` marker selects tests that can talk to Google Drive. The default
pytest selection and CI exclude it. The
[runner](../scripts/e2e_soak.sh) repeats the real-Drive suite the number of times
you give it and prints a summary.

When the suite is available, configure a dedicated disposable Drive folder and
the test account's authorized credentials in the local keyring. Replace the
placeholder with the sandbox folder ID before running the script. Live tests
may create and trash files in that folder.

```sh
NIMBO_E2E_FOLDER_ID="SANDBOX_FOLDER_ID" scripts/e2e_soak.sh 10
```

The runner reports an error if pytest collects no `e2e` tests.

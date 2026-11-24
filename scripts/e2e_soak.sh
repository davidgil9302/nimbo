#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 1 || ! $1 =~ ^[1-9][0-9]*$ ]] || ! printf -v repetitions '%d' "$1"; then
    printf 'Usage: %s NUMBER_OF_RUNS\n' "$0" >&2
    exit 2
fi

if [[ -z ${NIMBO_E2E_FOLDER_ID:-} ]]; then
    printf 'Set NIMBO_E2E_FOLDER_ID to a dedicated disposable Drive folder ID.\n' >&2
    exit 2
fi

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$repo_root"
if [[ ! -x .venv/bin/python ]]; then
    printf 'Create .venv and install the development dependencies first.\n' >&2
    exit 2
fi
export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"

passed=0
failed=0
for ((remaining = repetitions; remaining > 0; remaining--)); do
    round=$((repetitions - remaining + 1))
    printf '\nDrive e2e run %d of %d\n' "$round" "$repetitions"
    if .venv/bin/python -m pytest -m e2e tests/e2e; then
        passed=$((passed + 1))
    else
        status=$?
        failed=$((failed + 1))
        case "$status" in
            5)
                printf 'No real-Drive e2e tests were collected.\n' >&2
                break
                ;;
            2|3|4|130)
                printf 'Stopping after pytest exited with status %d.\n' "$status" >&2
                break
                ;;
        esac
    fi
done

printf '\nDrive e2e soak: %d passed, %d failed, %d unrun.\n' \
    "$passed" "$failed" "$((repetitions - passed - failed))"
((failed == 0))

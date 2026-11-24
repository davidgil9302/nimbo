"""Read stored run summaries before exposing them to CLI and dashboard rendering."""

from nimbo.core.errors import NimboError
from nimbo.core.index import Index
from nimbo.core.model import RunSummary


def _check_run(run: RunSummary) -> None:
    if not all(
        isinstance(value, str)
        for value in (run.reason, run.detail, run.started_at, run.finished_at, run.trigger)
    ) or (run.fingerprint is not None and not isinstance(run.fingerprint, str)):
        raise ValueError("Invalid run metadata")
    counters = (
        run.mkdirs,
        run.uploaded,
        run.uploaded_bytes,
        run.updated,
        run.touched,
        run.moved,
        run.adopted,
        run.trashed,
    )
    if any(type(count) is not int or not 0 <= count <= 2**63 - 1 for count in counters):
        raise ValueError("Invalid run counter")
    if run.run_id is not None and (type(run.run_id) is not int or run.run_id <= 0):
        raise ValueError("Invalid run ID")
    if not isinstance(run.trash_preview, list) or not all(
        isinstance(path, str) for path in run.trash_preview
    ):
        raise ValueError("Invalid deletion preview")
    for path, code in run.failed:
        if not isinstance(path, str) or not isinstance(code, str):
            raise ValueError("Invalid operation failure")
    for issue in run.warnings:
        if not all(isinstance(value, str) for value in (issue.code, issue.rel, issue.detail)):
            raise ValueError("Invalid run warning")


def read_run_summaries(index: Index, *, limit: int) -> list[RunSummary]:
    """Read and validate recent runs, raising a diagnostic for a damaged record."""
    try:
        runs = index.list_runs(limit=limit)
        for run in runs:
            _check_run(run)
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise NimboError(code="run_history_damaged") from exc
    return runs

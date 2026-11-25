"""Resolve the Drive identities and parent links of a plan before journaling it."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from .errors import NimboError
from .model import Op, OpKind, Plan

_CREATE = {OpKind.MKDIR, OpKind.UPLOAD}
_WITH_PARENT = _CREATE | {OpKind.MOVE}
_MAX_IDS = 1_000


def allocate(
    plan: Plan,
    root_id: str,
    generate: Callable[[int], list[str]],
    reuse: Callable[[Op], str | None] = lambda op: None,
) -> None:
    """Assign create IDs and resolve destination parents in place.

    Resolve each upload's parent before passing it to `reuse`. Request at
    most 1,000 new IDs per batch. Raise NimboError (plan_inconsistent) for
    invalid IDs or unresolvable parents, leaving the plan unchanged on failure.
    """
    folder_ids = {**plan.folder_ids, "": root_id}
    mkdirs: dict[str, int] = {}
    creates: dict[int, str] = {}
    for i, op in enumerate(plan.ops):
        if op.kind is OpKind.MKDIR:
            if op.rel in folder_ids or op.rel in mkdirs:
                raise NimboError(op.rel, code="plan_inconsistent")
            mkdirs[op.rel] = i

    for op in plan.ops:
        if op.kind in _WITH_PARENT and (
            op.parent_rel is None or not (folder_ids.get(op.parent_rel) or op.parent_rel in mkdirs)
        ):
            raise NimboError(op.rel, code="plan_inconsistent")

    used = set(folder_ids.values()) | {
        op.file_id for op in plan.ops if op.kind not in _CREATE and op.file_id
    }
    for kind in (OpKind.MKDIR, OpKind.UPLOAD):
        missing = []
        for i, op in enumerate(plan.ops):
            if op.kind is kind:
                file_id = op.file_id
                if kind is OpKind.UPLOAD:
                    assert op.parent_rel is not None
                    file_id = reuse(replace(op, parent_id=folder_ids[op.parent_rel])) or file_id
                if not file_id:
                    missing.append(i)
                elif file_id in used:
                    raise NimboError(op.rel, code="plan_inconsistent")
                else:
                    creates[i] = file_id
                    used.add(file_id)

        for offset in range(0, len(missing), _MAX_IDS):
            batch = missing[offset : offset + _MAX_IDS]
            ids = generate(len(batch))
            if (
                len(ids) != len(batch)
                or "" in ids
                or len(set(ids)) != len(ids)
                or used.intersection(ids)
            ):
                raise NimboError("Invalid generated IDs", code="plan_inconsistent")
            creates.update(zip(batch, ids, strict=True))
            used.update(ids)

        if kind is OpKind.MKDIR:
            folder_ids.update((rel, creates[i]) for rel, i in mkdirs.items())

    for i, op in enumerate(plan.ops):
        if i in creates:
            op.file_id = creates[i]
        if op.kind in _WITH_PARENT:
            assert op.parent_rel is not None
            op.parent_id = folder_ids[op.parent_rel]

"""Exercise id allocation before any plan is persisted."""

from __future__ import annotations

import pytest

from nimbo.core.allocate import allocate
from nimbo.core.errors import NimboError
from nimbo.core.model import Op, OpKind, Plan, PlanStats


def test_ids_assigned_in_batches() -> None:
    calls: list[int] = []
    plan = Plan(
        [Op(OpKind.UPLOAD, f"file-{i}", "new", parent_rel="") for i in range(2010)],
        PlanStats(),
    )

    def generate(count: int) -> list[str]:
        calls.append(count)
        return [f"id-{len(calls)}-{i}" for i in range(count)]

    allocate(plan, "root", generate)
    assert calls == [1000, 1000, 10]
    assert len({op.file_id for op in plan.ops}) == 2010
    assert {op.parent_id for op in plan.ops} == {"root"}


def test_parents_resolved_from_folder_ids_and_new_mkdirs() -> None:
    folder = Op(OpKind.MKDIR, "Docs/new", "new", parent_rel="Docs")
    upload = Op(OpKind.UPLOAD, "Docs/new/note", "new", parent_rel=folder.rel)
    move = Op(OpKind.MOVE, "Docs/new/report", "renamed", "report", folder.rel)
    plan = Plan([folder, upload, move], PlanStats(), folder_ids={"Docs": "docs-id"})

    ids = iter(["new-folder", "new-note"])
    allocate(plan, "root", lambda count: [next(ids) for _ in range(count)])

    assert (folder.file_id, folder.parent_id) == ("new-folder", "docs-id")
    assert (upload.file_id, upload.parent_id) == ("new-note", "new-folder")
    assert (move.file_id, move.parent_id) == ("report", "new-folder")


def test_session_ids_reused() -> None:
    plan = Plan(
        [
            Op(OpKind.UPLOAD, "resume", "new", parent_rel=""),
            Op(OpKind.UPLOAD, "fresh", "new", parent_rel=""),
        ],
        PlanStats(),
    )

    def generate(count: int) -> list[str]:
        assert count == 1
        return ["fresh-id"]

    def reuse(op: Op) -> str | None:
        assert op.parent_id == "root"
        return "saved-id" if op.rel == "resume" else None

    allocate(plan, "root", generate, reuse=reuse)
    assert [op.file_id for op in plan.ops] == ["saved-id", "fresh-id"]


def test_session_lookup_receives_parent_of_new_mkdir() -> None:
    folder = Op(OpKind.MKDIR, "Docs", "new", parent_rel="")
    upload = Op(OpKind.UPLOAD, "Docs/file", "new", parent_rel="Docs")
    plan = Plan([folder, upload], PlanStats())
    calls = []

    def generate(count: int) -> list[str]:
        calls.append(count)
        return ["docs-id"]

    def reuse(op: Op) -> str | None:
        assert op.kind is OpKind.UPLOAD
        assert op.parent_id == "docs-id"
        return "saved-id"

    allocate(plan, "root", generate, reuse=reuse)
    assert calls == [1]
    assert (upload.parent_id, upload.file_id) == ("docs-id", "saved-id")


@pytest.mark.parametrize("parent", [None, "missing"])
def test_unresolvable_parent_raises(parent: str | None) -> None:
    plan = Plan([Op(OpKind.UPLOAD, "missing/file", "new", parent_rel=parent)], PlanStats())

    with pytest.raises(NimboError) as exc:
        allocate(plan, "root", lambda _: pytest.fail("Invalid plan must not reserve IDs"))

    assert exc.value.code == "plan_inconsistent"


@pytest.mark.parametrize("ids", [[], ["same", "same"], ["valid", ""]])
def test_invalid_generated_ids_leave_plan_unallocated(ids: list[str]) -> None:
    plan = Plan(
        [Op(OpKind.UPLOAD, name, "new", parent_rel="") for name in ("a", "b")],
        PlanStats(),
    )
    with pytest.raises(NimboError) as exc:
        allocate(plan, "root", lambda _: ids)

    assert exc.value.code == "plan_inconsistent"
    assert all(op.file_id is None and op.parent_id is None for op in plan.ops)

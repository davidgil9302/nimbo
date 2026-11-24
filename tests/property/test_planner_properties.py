"""Check convergence, idempotence and safety over independently mutated mirrors."""

from collections import Counter
from dataclasses import replace
from random import Random

from hypothesis import given
from hypothesis import strategies as st

from nimbo.core.model import (
    OWNER_PROPERTY,
    LocalEntry,
    OpKind,
    Plan,
    PlanIssue,
    RemoteFile,
    drive_ms,
    is_under,
    rel_parent,
    rel_top,
)
from nimbo.core.planner import PlannerOptions, make_plan
from nimbo.core.tree import RemoteTree
from tests.property.sim import apply_plan, expected_view
from tests.property.strategies import FOLDERS, MATCHER, OWNER, ROOT, Scenario, scenarios


def _plan(case: Scenario, *, deep: bool = True) -> Plan:
    return make_plan(
        case.local,
        RemoteTree(ROOT, case.remote.values()),
        MATCHER,
        lambda entry: case.md5(entry.rel),
        PlannerOptions(FOLDERS, OWNER, deep=deep, protected=case.protected),
    )


def _apply(case: Scenario, plan: Plan) -> dict[str, RemoteFile]:
    assert not plan.errors
    return apply_plan(case.remote, plan, case.local, case.md5, ROOT, OWNER)


def _protected(rel: str, case: Scenario) -> bool:
    return any(is_under(rel, protected) for protected in case.protected)


def _untouchable(case: Scenario, rel: str, item: RemoteFile) -> bool:
    return (
        rel_top(rel) not in FOLDERS
        or item.is_google_native
        or MATCHER.excluded(rel, item.is_folder)
        or _protected(rel, case)
    )


def _ancestors_of_untouchables(case: Scenario, before: RemoteTree) -> set[str]:
    retained: set[str] = set()
    for rel, item in before.walk():
        if rel_top(rel) in FOLDERS and _untouchable(case, rel, item):
            (parent_id,) = item.parents
            while parent_id != ROOT and parent_id not in retained:
                retained.add(parent_id)
                parent = before.get(parent_id)
                assert parent is not None
                (parent_id,) = parent.parents
    return retained


@given(scenarios())
def test_apply_converges_to_mirror(case: Scenario) -> None:
    """The eligible snapshot matches the local bytes, sizes, timestamps and paths."""
    plan = _plan(case)
    applied = _apply(case, plan)
    for op in plan.ops:
        if op.kind is OpKind.TRASH:
            assert op.mtime_ns == 0
        else:
            entry = case.local[op.rel]
            assert op.mtime_ns == (0 if entry.is_dir else entry.mtime_ns)
            if op.kind is OpKind.MOVE and entry.is_dir:
                assert op.file_id is not None
                assert applied[op.file_id].modified_ms == case.remote[op.file_id].modified_ms
    after = RemoteTree(ROOT, applied.values())
    desired = {
        rel: entry
        for rel, entry in expected_view(case.local, MATCHER, FOLDERS).items()
        if not _protected(rel, case)
    }
    before = RemoteTree(ROOT, case.remote.values())
    retained_ancestors = _ancestors_of_untouchables(case, before)

    mirror = {}
    for rel, item in after.walk():
        if _untouchable(case, rel, item):
            continue
        if item.is_folder:
            entry = desired.get(rel)
            if entry is None or not entry.is_dir:
                assert item.id in retained_ancestors
                continue
            mirror[rel] = (True, 0, None, None)
        elif item.owner_root == OWNER:
            assert rel not in mirror
            mirror[rel] = (False, item.size, item.md5, item.modified_ms)
        else:
            # The policy retains unmanaged duplicates at a path with a local match.
            assert rel in desired
            assert item == case.remote[item.id]
    assert mirror == {
        rel: (
            entry.is_dir,
            entry.size,
            None if entry.is_dir else case.md5(rel),
            None if entry.is_dir else drive_ms(entry.mtime_ns),
        )
        for rel, entry in desired.items()
    }


@given(scenarios())
def test_second_plan_is_empty(case: Scenario) -> None:
    """Executing a plan reaches a fixed point even in deep comparison mode."""
    after = _apply(case, _plan(case))
    assert _plan(replace(case, remote=after)).is_empty


@given(scenarios(), st.integers())
def test_plan_is_deterministic(case: Scenario, seed: int) -> None:
    """Input order has no effect on operations, diagnostics, counts or the approval hash."""
    random = Random(seed)
    local = list(case.local.items())
    remote = list(case.remote.items())
    folders = list(FOLDERS)
    protected = list(case.protected)
    for values in (local, remote, folders, protected):
        random.shuffle(values)
    first = _plan(case)
    second = make_plan(
        dict(local),
        RemoteTree(ROOT, dict(remote).values()),
        MATCHER,
        lambda entry: case.md5(entry.rel),
        PlannerOptions(tuple(folders), OWNER, deep=True, protected=tuple(protected)),
    )
    assert first.ops == second.ops
    assert first == second
    assert first.fingerprint() == second.fingerprint()


@given(scenarios())
def test_untouchables_never_touched(case: Scenario) -> None:
    """Neither explicit operations nor inherited folder changes reach untouchable IDs."""
    plan = _plan(case)
    before = RemoteTree(ROOT, case.remote.values())
    after = RemoteTree(ROOT, _apply(case, plan).values())
    untouchable_ids = set()
    for rel, item in before.walk():
        if _untouchable(case, rel, item):
            untouchable_ids.add(item.id)
            assert after.get(item.id) == item
            assert after.rel_of(item.id) == rel
    for op in plan.ops:
        assert op.file_id not in untouchable_ids
        if op.kind in (OpKind.MOVE, OpKind.TRASH) and op.file_id is not None:
            assert not any(item.id in untouchable_ids for item in before.descendants(op.file_id))


@given(scenarios())
def test_no_op_targets_outside_scope(case: Scenario) -> None:
    """All existing IDs and destinations in a write belong to an active folder."""
    before = RemoteTree(ROOT, case.remote.values())
    for op in _plan(case).ops:
        assert rel_top(op.rel) in FOLDERS
        if op.file_id is not None:
            src = before.rel_of(op.file_id)
            assert src is not None and rel_top(src) in FOLDERS
        if op.parent_rel:
            assert rel_top(op.parent_rel) in FOLDERS


@given(scenarios(allow_unmanaged_duplicates=False))
def test_no_duplicate_paths_after_apply(case: Scenario) -> None:
    """A local file may coexist with exactly the folder needed by an untouchable descendant."""
    plan = _plan(case)
    after = RemoteTree(ROOT, _apply(case, plan).values())
    before = RemoteTree(ROOT, case.remote.values())
    retained = _ancestors_of_untouchables(case, before)
    for rel, group in after.by_rel().items():
        entry = case.local.get(rel)
        kept = [item for item in group if item.is_folder and item.id in retained]
        if kept and entry is not None and not entry.is_dir and not _protected(rel, case):
            (folder,) = kept
            (mirrored,) = [item for item in group if item.id != folder.id]
            assert before.get(folder.id) == folder
            assert not mirrored.is_folder and mirrored.owner_root == OWNER
            assert mirrored.md5 == case.md5(rel)
            assert PlanIssue("type-conflict-kept", rel) in plan.warnings
        else:
            assert len(group) == 1


@given(scenarios())
def test_moves_only_when_content_identical(case: Scenario) -> None:
    """File moves and files carried without an UPDATE preserve identical bytes."""
    plan = _plan(case, deep=False)
    before = RemoteTree(ROOT, case.remote.values())
    updates = {op.file_id for op in plan.ops if op.kind is OpKind.UPDATE}
    for op in plan.ops:
        if op.kind is not OpKind.MOVE:
            continue
        assert op.file_id is not None and op.src_rel is not None
        item = case.remote[op.file_id]
        if item.is_folder:
            for child in before.descendants(item.id):
                if not child.is_folder:
                    source = before.rel_of(child.id)
                    assert source is not None
                    # A nested folder can be moved out before its ancestor.
                    if any(
                        move.kind is OpKind.MOVE
                        and move.src_rel
                        and is_under(source, move.src_rel)
                        and is_under(move.src_rel, op.src_rel)
                        and move is not op
                        for move in plan.ops[: plan.ops.index(op)]
                    ):
                        continue
                    dest = op.rel + source[len(op.src_rel) :]
                    identical = (child.size, child.md5) == (case.local[dest].size, case.md5(dest))
                    assert identical or child.id in updates
        else:
            assert item.size == case.local[op.rel].size > 0
            assert item.md5 == op.md5 == case.md5(op.rel)


@given(scenarios())
def test_protected_subtrees_are_untouched(case: Scenario) -> None:
    """A protected root keeps its physical subtree even if its local path changed."""
    protected = (*case.protected, "Projects/Old folder")
    case = replace(case, protected=protected)
    plan = _plan(case)
    for op in plan.ops:
        assert not _protected(op.rel, case)
    after = RemoteTree(ROOT, _apply(case, plan).values())
    for rel, item in RemoteTree(ROOT, case.remote.values()).walk():
        if _protected(rel, case):
            assert after.get(item.id) == item
            assert after.rel_of(item.id) == rel


@given(scenarios(), st.integers(min_value=0, max_value=50))
def test_foreign_owner_preserves_physical_branch(case: Scenario, index: int) -> None:
    """A foreign tag protects the item and its physical descendants."""
    in_scope = [
        (rel, item)
        for rel, item in RemoteTree(ROOT, case.remote.values()).walk()
        if rel_top(rel) in FOLDERS
    ]
    rel, item = in_scope[index % len(in_scope)]
    foreign = replace(item, properties={OWNER_PROPERTY: "foreign"}, owned_by_me=True)
    case = replace(case, remote={**case.remote, item.id: foreign})
    plan = _plan(case)
    assert PlanIssue("foreign-item-kept", rel) in plan.warnings
    before = RemoteTree(ROOT, case.remote.values())
    after = RemoteTree(ROOT, _apply(case, plan).values())
    for protected in (foreign, *before.descendants(foreign.id)):
        assert after.get(protected.id) == protected
        assert after.rel_of(protected.id) == before.rel_of(protected.id)


@given(scenarios())
def test_destination_parents_resolve_in_phase_order(case: Scenario) -> None:
    """Parent mappings resolve in order, and the physical destination matches after apply."""
    plan = _plan(case)
    resolved = set(plan.folder_ids)
    assert plan.folder_ids[""] == ROOT
    assert [op.phase for op in plan.ops] == sorted(op.phase for op in plan.ops)
    for op in plan.ops:
        if op.kind in (OpKind.MKDIR, OpKind.UPLOAD, OpKind.MOVE):
            assert op.parent_rel == rel_parent(op.rel)
            assert op.parent_rel in resolved
            if op.kind is OpKind.MKDIR:
                resolved.add(op.rel)
    after = RemoteTree(ROOT, _apply(case, plan).values())
    for rel, folder_id in plan.folder_ids.items():
        if folder_id in after:
            assert after.rel_of(folder_id) == rel


@given(scenarios())
def test_hasher_is_memoized_for_each_entry(case: Scenario) -> None:
    """One entry is never hashed twice, including when an attempted read returns None."""
    calls: Counter[str] = Counter()

    def md5_of(entry: LocalEntry) -> str | None:
        assert not entry.is_dir
        calls[entry.rel] += 1
        return None if entry.size % 2 else case.md5(entry.rel)

    make_plan(
        case.local,
        RemoteTree(ROOT, case.remote.values()),
        MATCHER,
        md5_of,
        PlannerOptions(FOLDERS, OWNER, deep=True, protected=case.protected),
    )
    assert all(count == 1 for count in calls.values())

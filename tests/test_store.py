import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from sra.errors import ConflictError
from sra.models import Selection
from sra.store import Store


def claim(store, spec, *, retry=False):
    return store.claim("example/research", 1, spec, "policy", Selection(), "/tmp/work", "sra/issue-1", "a"*40,
                       retry=retry, max_attempts=3)


def test_approval_is_required(tmp_path, spec):
    store = Store(tmp_path / "state")
    with pytest.raises(ConflictError):
        claim(store, spec)


def test_duplicate_claim_is_atomic(tmp_path, spec):
    store = Store(tmp_path / "state")
    store.approve("example/research", 1, spec, "policy")
    def attempt(_):
        try:
            return claim(store, spec).id
        except ConflictError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert sum(x is not None for x in results) == 1


def test_state_transition_and_explicit_retry(tmp_path, spec):
    store = Store(tmp_path / "state")
    store.approve("example/research", 1, spec, "policy")
    first = claim(store, spec)
    with pytest.raises(ConflictError):
        store.update(first.id, state="awaiting_review")
    store.update(first.id, state="running")
    store.update(first.id, state="verifying")
    store.update(first.id, state="awaiting_review")
    with pytest.raises(ConflictError):
        claim(store, spec)
    second = claim(store, spec, retry=True)
    assert second.attempt == 2


def test_mutated_spec_needs_revision_and_reapproval(tmp_path, spec):
    store = Store(tmp_path / "state")
    store.approve("example/research", 1, spec, "policy")
    revised = spec.model_copy(update={"objective": "Changed"})
    assert not store.approved("example/research", 1, revised, "policy")
    with pytest.raises(ConflictError):
        store.approve("example/research", 1, revised, "policy")
    revised = revised.model_copy(update={"revision": 2})
    store.approve("example/research", 1, revised, "policy")
    assert store.approved("example/research", 1, revised, "policy")


def test_recovery_never_steals_live_pid(tmp_path, spec):
    store = Store(tmp_path / "state")
    store.approve("example/research", 1, spec, "policy")
    first = claim(store, spec)
    assert first.owner_pid == os.getpid()
    with pytest.raises(ConflictError, match="still exists"):
        store.recover(first.id)


def test_no_two_issues_for_same_spec_id(tmp_path, spec):
    store = Store(tmp_path / "state")
    store.approve("example/research", 1, spec, "policy")
    with pytest.raises(ConflictError):
        store.approve("example/research", 2, spec, "policy")

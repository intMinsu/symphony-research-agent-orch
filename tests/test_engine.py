import json
from pathlib import Path

import pytest

from sra.config import load_project
from sra.engine import Orchestrator
from sra.errors import ConflictError, ProcessError, SRAError
from sra.models import RunnerConfig
from sra.specs import issue_body
from sra.store import Store
from sra.workspace import git


def engine_for(project, tmp_path, tracker):
    return Orchestrator(project, Store(tmp_path / "state"), tracker)


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_issue_to_workspace_to_verified_result(project, spec, tracker, tmp_path, provider):
    engine = engine_for(project, tmp_path, tracker)
    engine.publish(spec, ready=True)
    source_head = git(project.root, "rev-parse", "HEAD")
    run = engine.run(1, trust_project=True, provider=provider)
    assert run.state == "awaiting_review"
    assert run.provider == provider
    assert run.evidence.checks[0].returncode == 0
    assert Path(run.workspace, "docs/specs/EXP-001.md").exists()
    assert not (project.root / "src/result.py").exists()
    assert git(project.root, "rev-parse", "HEAD") == source_head
    assert git(project.root, "status", "--porcelain") == ""
    assert run.pr_url is None  # Delivery is opt-in.
    assert tracker.workpads and "Human acceptance" in tracker.workpads[-1]


def test_rework_switches_provider_without_reusing_native_session(project, spec, tracker, tmp_path):
    engine = engine_for(project, tmp_path, tracker)
    engine.publish(spec, ready=True)
    first = engine.run(1, trust_project=True, provider="codex")
    second = engine.run(1, trust_project=True, provider="claude", retry=True, feedback="Review the existing fixture")
    assert first.workspace == second.workspace and first.branch == second.branch
    assert second.attempt == 2
    assert first.session_id == "codex-session" and second.session_id == "claude-session"


def test_label_is_not_authorization(project, tracker, tmp_path):
    engine = engine_for(project, tmp_path, tracker)
    with pytest.raises(ConflictError, match="approved"):
        engine.run(1, trust_project=True)


def test_trust_is_explicit(project, spec, tracker, tmp_path):
    engine = engine_for(project, tmp_path, tracker)
    engine.publish(spec, ready=True)
    with pytest.raises(SRAError, match="trust-project"):
        engine.run(1, trust_project=False)


def test_remote_spec_mutation_blocks_dispatch(project, spec, tracker, tmp_path):
    engine = engine_for(project, tmp_path, tracker)
    engine.publish(spec, ready=True)
    tracker.item["body"] = issue_body(spec.model_copy(update={"revision": 2, "objective": "Unreviewed change"}))
    with pytest.raises(ConflictError):
        engine.run(1, trust_project=True)


def test_closed_issue_is_not_run(project, spec, tracker, tmp_path):
    engine = engine_for(project, tmp_path, tracker)
    engine.publish(spec, ready=True)
    tracker.item["state"] = "closed"
    with pytest.raises(ConflictError):
        engine.run(1, trust_project=True)


def test_dirty_source_never_gets_modified(project, spec, tracker, tmp_path):
    engine = engine_for(project, tmp_path, tracker)
    engine.publish(spec, ready=True)
    (project.root / "uncommitted.txt").write_text("preserve me")
    with pytest.raises(SRAError, match="dirty"):
        engine.run(1, trust_project=True)
    assert (project.root / "uncommitted.txt").read_text() == "preserve me"
    assert engine.store.latest(engine.repository, 1).state == "blocked"


def test_delivery_is_host_owned_and_retryable(project, spec, tracker, tmp_path, monkeypatch):
    engine = engine_for(project, tmp_path, tracker)
    engine.publish(spec, ready=True)
    run = engine.run(1, trust_project=True)
    pushes = []
    monkeypatch.setattr(engine.workspaces, "push", lambda info, token, head: pushes.append(info.branch))
    delivered = engine.deliver(1, trust_project=True)
    assert delivered.pr_url.endswith("/pull/2")
    assert pushes == [run.branch]
    assert len(tracker.pr_calls) == 1
    assert "Human review" in tracker.pr_calls[0][3]
    assert git(Path(run.workspace), "status", "--porcelain") == ""


def test_delivery_detects_post_verification_edits(project, spec, tracker, tmp_path, monkeypatch):
    engine = engine_for(project, tmp_path, tracker)
    engine.publish(spec, ready=True)
    run = engine.run(1, trust_project=True)
    Path(run.workspace, "src/result.py").write_text("RESULT = 999\n")
    with pytest.raises(ConflictError, match="changed after"):
        engine.deliver(1, trust_project=True)
    assert not tracker.pr_calls


def test_push_failure_keeps_verified_commit_for_delivery_retry(project, spec, tracker, tmp_path, monkeypatch):
    engine = engine_for(project, tmp_path, tracker)
    engine.publish(spec, ready=True)
    engine.run(1, trust_project=True)
    def fail(info, token, head):
        raise SRAError("simulated network failure")
    monkeypatch.setattr(engine.workspaces, "push", fail)
    with pytest.raises(SRAError):
        engine.deliver(1, trust_project=True)
    record = engine.store.latest(engine.repository, 1)
    assert record.state == "awaiting_review" and record.evidence.head_sha
    monkeypatch.setattr(engine.workspaces, "push", lambda info, token, head: None)
    assert engine.deliver(1, trust_project=True).pr_url


def test_planning_produces_valid_unpublished_draft(project, tracker, tmp_path):
    engine = engine_for(project, tmp_path, tracker)
    before = tracker.item["body"]
    planned = engine.plan("Add a fixture", "PLAN-001", trust_project=True, provider="claude")
    assert planned.id == "PLAN-001"
    assert tracker.item["body"] == before
    assert not engine.store.list(engine.repository)


@pytest.mark.parametrize("mode,expected", [("scope", "outside the approved scope"), ("policy", "Protected path")])
def test_scope_and_policy_are_enforced(project, spec, tracker, tmp_path, mode, expected):
    import yaml
    config = project.workflow.model_dump(mode="json")
    config["runners"]["codex"]["command"] += ["--fake-mode", mode]
    path = project.root / "WORKFLOW.md"
    path.write_text("---\n" + yaml.safe_dump(config) + "---\n" + project.instructions)
    git(project.root, "add", "WORKFLOW.md")
    git(project.root, "commit", "-m", "Configure test runner")
    updated = load_project(project.root)
    engine = engine_for(updated, tmp_path, tracker)
    engine.publish(spec, ready=True)
    with pytest.raises(SRAError, match=expected):
        engine.run(1, trust_project=True)
    assert engine.store.latest(engine.repository, 1).state == "blocked"
    assert not tracker.pr_calls


def test_delivery_cannot_be_claimed_twice(project, spec, tracker, tmp_path):
    engine = engine_for(project, tmp_path, tracker)
    engine.publish(spec, ready=True)
    run = engine.run(1, trust_project=True)
    engine.store.update(run.id, state="delivering", expected_state="awaiting_review")
    with pytest.raises(ConflictError, match="Expected state"):
        engine.store.update(run.id, state="delivering", expected_state="awaiting_review")


def test_cancel_marker_stops_execution(project, spec, tracker, tmp_path, monkeypatch):
    engine = engine_for(project, tmp_path, tracker)
    engine.publish(spec, ready=True)
    real_prepare = engine.workspaces.prepare
    def prepare_then_cancel(info, item):
        real_prepare(info, item)
        record = engine.store.latest(engine.repository, 1)
        (engine.store.run_dir(record.id) / "cancel.request").write_text("cancel")
    monkeypatch.setattr(engine.workspaces, "prepare", prepare_then_cancel)
    with pytest.raises(ProcessError, match="cancelled"):
        engine.run(1, trust_project=True)
    assert engine.store.latest(engine.repository, 1).state == "cancelled"

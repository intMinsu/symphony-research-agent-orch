"""Deterministic coordination around a replaceable coding-agent process."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import Project, load_project, safe_read
from .errors import CompatibilityError, ConflictError, ProcessError, SRAError
from .github import GitHubTracker
from .models import ACTIVE, Limits, WorkSpec, digest, utcnow
from .process import execute
from .runners import Runner, redact
from .specs import parse_issue, planner_result, render_spec
from .store import Store
from .workspace import Workspaces, git


class Cancellation:
    def __init__(self, path: Path, external: threading.Event | None):
        self.path = path
        self.external = external

    def is_set(self) -> bool:
        return self.path.exists() or bool(self.external and self.external.is_set())


class Orchestrator:
    def __init__(self, project: Project, store: Store, tracker=None):
        if store.root.is_relative_to(project.root):
            raise SRAError("Runtime state must live outside the research repository")
        self.project = project
        self.store = store
        self.tracker = tracker or GitHubTracker(project.workflow.tracker)
        self.workspaces = Workspaces(project, store.root)
        self.repository = project.workflow.tracker.repository

    def _current_policy(self) -> None:
        if load_project(self.project.root).policy_sha != self.project.policy_sha:
            raise ConflictError("Project policy changed; restart/reload and approve the new policy hash")

    def _input(self, number: int, *, require_ready=True):
        issue = self.tracker.issue(number)
        if require_ready and not self.tracker.eligible(issue):
            raise ConflictError("Issue is closed, not labeled ready, or excluded by the author policy")
        spec = parse_issue(issue.get("body") or "")
        self.project.validate_spec(spec)
        return issue, spec

    def publish(self, spec: WorkSpec, *, ready=False) -> dict:
        self._current_policy()
        self.project.validate_spec(spec)
        with self.store.publish_lock(self.repository):
            issue = self.tracker.publish(spec, ready=ready)
            if ready:
                self.store.approve(self.repository, issue["number"], spec, self.project.policy_sha)
            return issue

    def approve(self, number: int, expected_sha: str) -> WorkSpec:
        self._current_policy()
        _, spec = self._input(number, require_ready=False)
        if digest(spec) != expected_sha:
            raise ConflictError("Issue spec changed or the supplied SHA-256 does not match")
        self.store.approve(self.repository, number, spec, self.project.policy_sha)
        self.tracker.ensure_label()
        self.tracker.request("POST", f"/issues/{number}/labels",
                             {"labels": [self.project.workflow.tracker.ready_label]})
        return spec

    def _workpad(self, record) -> None:
        # Never publish raw tool output, local file paths, or model transcripts.
        text = (f"Run: `{record.id}`\nState: **{record.state}**\n"
                f"Spec SHA-256: `{record.spec_sha256}`\nProvider: `{record.provider}`\n"
                f"Attempt: {record.attempt}\nUpdated: {record.updated_at}\n")
        for check in record.evidence.model_dump()["checks"]:
            text += f"\n- Check `{check['id']}`: exit {check['returncode']}"
        if record.pr_url:
            text += f"\n\nDraft PR: {record.pr_url}"
        text += "\n\nHuman acceptance review is required. Raw logs remain local.\n"
        try:
            self.tracker.workpad(record.issue_number, text)
        except SRAError as exc:
            path = self.store.run_dir(record.id) / "warnings.txt"
            with path.open("a", encoding="utf-8") as log:
                log.write(redact(str(exc)) + "\n")

    def run(self, number: int, *, trust_project: bool, provider=None, model=None, effort=None,
            retry=False, publish_pr=False, cancel: threading.Event | None = None, feedback: str | None = None):
        if not trust_project:
            raise SRAError("Pass --trust-project only for code, hooks and checks you trust; see SECURITY.md")
        self._current_policy()
        _, spec = self._input(number)
        previous = self.store.latest(self.repository, number)
        selection = self.project.selection(spec, provider=provider, model=model, effort=effort)
        info = self.workspaces.describe(number, spec)
        record = self.store.claim(self.repository, number, spec, self.project.policy_sha, selection,
                                  str(info.path), info.branch, info.base_sha, retry=retry,
                                  max_attempts=self.project.workflow.limits.max_attempts)
        directory = self.store.run_dir(record.id)
        cancel = Cancellation(directory / "cancel.request", cancel)
        (directory / "spec.json").write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
        (directory / "workflow.json").write_text(self.project.workflow.model_dump_json(indent=2) + "\n")
        (directory / "SPEC.md").write_text(render_spec(spec), encoding="utf-8")
        runner = Runner(selection, self.project.workflow.runners[selection.provider],
                        token_env=self.project.workflow.tracker.token_env)
        event_log = directory / "events.jsonl"

        def log_event(event):
            with event_log.open("a", encoding="utf-8") as log:
                log.write(event.model_dump_json() + "\n")
            if event.session_id:
                self.store.update(record.id, session_id=event.session_id)

        def started(pid):
            self.store.update(record.id, child_pid=pid)

        try:
            profile = runner.probe(self.project.root)
            (directory / "capabilities.json").write_text(profile.model_dump_json(indent=2) + "\n")
            runner.argv()  # Fail before allocating a workspace when a required surface is absent.
            self.workspaces.prepare(info, spec)
            if cancel.is_set():
                raise ProcessError("cancelled")
            self.store.update(record.id, state="running")
            self._workpad(self.store.get(record.id))
            handoff = ""
            if feedback:
                handoff += "\nReviewed feedback for this attempt (within approved scope):\n" + feedback
            if previous:
                report = self.store.run_dir(previous.id) / "result.txt"
                if report.exists():
                    handoff += "\nPrevious attempt's untrusted summary (re-check against the spec):\n" + safe_read(report)[-12000:]
            prompt = ("Execute the approved WorkSpec in this repository. Read AGENTS.md and/or CLAUDE.md "
                      "and .agent/POLICY.md when present. Do not change the approved spec or policy. "
                      "Do not commit, push, create/close issues, create/merge PRs, or start detached jobs. "
                      "The host owns delivery. Do not broaden scope or change acceptance criteria. "
                      "A previous attempt may have partial work; inspect before repeating experiments. "
                      "Report completed criteria, exact evidence, limitations, and blockers.\n\n"
                      "Repository workflow:\n" + self.project.instructions + "\n\n" + render_spec(spec) + handoff)
            result = runner.run(prompt, info.path, self.project.workflow.limits, cancel=cancel,
                                on_event=log_event, on_start=started,
                                heartbeat=lambda: self.store.update(record.id))
            (directory / "result.txt").write_text(result.text + "\n", encoding="utf-8")
            self.store.update(record.id, state="verifying", child_pid=None, session_id=result.session_id)
            # Scope checks before running host commands; commands come only from trusted project config.
            self.workspaces.fingerprint(info, spec)
            checks = []
            check_ids = list(dict.fromkeys(self.project.workflow.required_checks + spec.check_ids))
            for check_id in check_ids:
                check = self.project.workflow.checks[check_id]
                checked = execute(check.argv, cwd=info.path, env=runner.env, timeout=check.timeout_seconds,
                                  max_bytes=self.project.workflow.limits.max_output_bytes, cancel=cancel,
                                  on_start=started, heartbeat=lambda: self.store.update(record.id))
                (directory / f"check-{check_id}.txt").write_text(
                    redact(checked.stdout + "\n--- stderr ---\n" + checked.stderr), encoding="utf-8")
                checks.append({"id": check_id, "returncode": checked.returncode})
                self.store.update(record.id, child_pid=None, evidence={"checks": checks})
                if checked.returncode:
                    raise ProcessError(f"Required check {check_id} failed with exit {checked.returncode}")
            paths, fingerprint = self.workspaces.fingerprint(info, spec)
            evidence = {"checks": checks, "changed_files": paths, "fingerprint": fingerprint,
                        "usage": result.usage, "cli_version": result.capabilities.version,
                        "verified_at": utcnow(), "human_acceptance_required": True}
            self._current_policy()
            _, latest_spec = self._input(number)
            if digest(latest_spec) != digest(spec):
                raise ConflictError("Remote spec changed during execution; no delivery was attempted")
            self.store.update(record.id, state="awaiting_review", evidence=evidence, child_pid=None)
            if publish_pr and spec.delivery == "draft_pr":
                return self.deliver(number, trust_project=True)
            self._workpad(self.store.get(record.id))
            return self.store.get(record.id)
        except BaseException as exc:
            current = self.store.get(record.id)
            if current.state in ACTIVE:
                state = "blocked" if isinstance(exc, (CompatibilityError, ConflictError, SRAError)) else "failed"
                if isinstance(exc, ProcessError):
                    state = "cancelled" if str(exc) == "cancelled" else "failed"
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    state = "cancelled"
                self.store.update(record.id, state=state, error=redact(str(exc))[:4000], child_pid=None)
                self._workpad(self.store.get(record.id))
            raise

    def deliver(self, number: int, *, trust_project: bool):
        if not trust_project:
            raise SRAError("Delivery requires --trust-project and an explicit `deliver` or --publish-pr")
        self._current_policy()
        _, spec = self._input(number)
        record = self.store.latest(self.repository, number)
        if not record or record.state != "awaiting_review":
            raise ConflictError("Delivery requires the latest run to be awaiting_review")
        if spec.delivery != "draft_pr":
            raise SRAError("This spec requests a local research report, not a PR")
        if not self.store.approved(self.repository, number, spec, self.project.policy_sha):
            raise ConflictError("Current contract is not locally approved")
        if digest(spec) != record.spec_sha256 or record.policy_sha256 != self.project.policy_sha:
            raise ConflictError("Verified evidence belongs to a different spec/policy")
        info = self.workspaces.describe(number, spec)
        if str(info.path) != record.workspace or info.base_sha != record.base_sha:
            raise ConflictError("Workspace metadata changed after verification")
        _, fingerprint = self.workspaces.fingerprint(info, spec)
        if fingerprint != record.evidence.fingerprint:
            raise ConflictError("Workspace changed after verification; use explicit --retry to revalidate")
        self.store.update(record.id, state="delivering", expected_state="awaiting_review", owner_pid=os.getpid())
        try:
            # Commit is local. Update the fingerprint BEFORE the remote side effect for safe retry.
            head = self.workspaces.commit(info, spec, fingerprint)
            _, committed_fingerprint = self.workspaces.fingerprint(info, spec)
            evidence = {**record.evidence.model_dump(mode="json"), "head_sha": head, "fingerprint": committed_fingerprint}
            self.store.update(record.id, evidence=evidence)
            self.workspaces.push(info, self.tracker.token, head)
            checks = "\n".join(f"- `{x['id']}`: exit {x['returncode']}" for x in evidence.get("checks", []))
            body = (f"Implements #{number}\n\nSpec: `docs/specs/{spec.id}.md`\n"
                    f"Spec SHA-256: `{digest(spec)}`\nVerified commit: `{head}`\n\n"
                    f"## Validation\n{checks or 'No automated checks configured.'}\n\n"
                    "## Review required\nHuman review of every acceptance criterion and research claim is required. "
                    "SRA does not auto-merge or auto-close the Issue.\n")
            pr = self.tracker.pull_request(info.branch, self.project.workflow.base_ref, spec.title, body)
            self.store.update(record.id, state="awaiting_review", pr_url=pr["html_url"], error=None)
        except BaseException as exc:
            self.store.update(record.id, state="awaiting_review", error=redact(str(exc))[:4000])
            raise
        finally:
            self._workpad(self.store.get(record.id))
        return self.store.get(record.id)

    def plan(self, request: str, spec_id: str, *, trust_project: bool, provider=None, model=None, effort=None) -> WorkSpec:
        if not trust_project:
            raise SRAError("Planning executes a local CLI; pass --trust-project for a trusted repository")
        self._current_policy()
        selection = self.project.selection(planner=True, provider=provider, model=model, effort=effort)
        runner = Runner(selection, self.project.workflow.runners[selection.provider],
                        token_env=self.project.workflow.tracker.token_env)
        log_dir = self.store.root / "planning"
        log_dir.mkdir(exist_ok=True, mode=0o700)
        plan_log = log_dir / uuid.uuid4().hex
        plan_log.mkdir(mode=0o700)
        profile = runner.probe(self.project.root)
        (plan_log / "capabilities.json").write_text(profile.model_dump_json(indent=2) + "\n")
        runner.argv(read_only=True)
        def planning_event(event):
            with (plan_log / "events.jsonl").open("a", encoding="utf-8") as log:
                log.write(event.model_dump_json() + "\n")
        with tempfile.TemporaryDirectory(prefix="plan-", dir=log_dir) as temporary:
            workspace = Path(temporary) / "repo"
            git(Path(temporary), "clone", "--no-local", "--no-hardlinks", str(self.project.root), str(workspace))
            git(workspace, "checkout", "--detach", self.project.workflow.base_ref)
            git(workspace, "remote", "remove", "origin")
            policy_path = self.project.root / ".agent/PLANNING.md"
            policy = safe_read(policy_path) if policy_path.exists() else ""
            prompt = ("You are a research planning agent. Inspect the repository without editing files or running "
                      "experiments. Return exactly one JSON object matching the WorkSpec schema below; no prose. "
                      "Produce one bounded task. Use the requested id and revision 1. Evidence must be observable. "
                      "Do not invent completed results. check_ids may only select configured checks. "
                      "Do not create issues or PRs. A human will review this draft.\n\n"
                      f"Requested id: {spec_id}\nAllowed check IDs: {list(self.project.workflow.checks)}\n"
                      f"Planning policy:\n{policy}\nRequest:\n{request}\nSchema:\n"
                      + json.dumps(WorkSpec.model_json_schema()))
            try:
                result = runner.run(prompt, workspace, self.project.workflow.limits, read_only=True,
                                    on_event=planning_event)
                (plan_log / "candidate.txt").write_text(result.text, encoding="utf-8")
                spec = planner_result(result.text)
            except SRAError as exc:
                raise SRAError(f"{exc}; planning logs: {plan_log}") from exc
            if spec.id != spec_id:
                raise SRAError("Planner changed the requested spec id")
            self.project.validate_spec(spec)
            return spec

    def watch(self, *, trust_project: bool, once=False, publish_pr=False, on_status=print) -> None:
        if not trust_project:
            raise SRAError("watch requires --trust-project; only locally approved contracts are dispatchable")
        stopped = threading.Event()
        active = {}
        dispatched_once = False
        failure_count = 0
        limit = self.project.workflow.limits.max_concurrent_runs
        with ThreadPoolExecutor(max_workers=limit) as pool:
            try:
                while not stopped.is_set():
                    self._current_policy()
                    for issue_number, (future, cancel) in list(active.items()):
                        if future.done():
                            try:
                                record = future.result()
                                on_status(f"Issue #{issue_number}: {record.state} ({record.id})")
                            except Exception as exc:
                                on_status(f"Issue #{issue_number}: {redact(str(exc))}")
                            del active[issue_number]
                    try:
                        candidates = self.tracker.candidates()
                        eligible_ids = {x["number"] for x in candidates}
                        for issue_number, (_, cancel) in active.items():
                            if issue_number not in eligible_ids:
                                cancel.set()
                        failure_count = 0
                    except SRAError as exc:
                        failure_count += 1
                        on_status(f"Tracker unavailable; no new dispatch: {redact(str(exc))}")
                        stopped.wait(min(300, self.project.workflow.limits.poll_seconds * 2 ** min(failure_count, 4)))
                        continue
                    for item in candidates:
                        if len(active) >= limit or (once and dispatched_once):
                            break
                        number = item["number"]
                        if number in active:
                            continue
                        try:
                            spec = parse_issue(item.get("body") or "")
                            self.project.validate_spec(spec)
                        except (SRAError, ValueError):
                            continue
                        if not self.store.approved(self.repository, number, spec, self.project.policy_sha):
                            continue
                        previous = self.store.latest(self.repository, number)
                        if previous and (previous.state in ACTIVE or previous.spec_sha256 == digest(spec)):
                            continue
                        cancel = threading.Event()
                        future = pool.submit(self.run, number, trust_project=True,
                                             publish_pr=publish_pr, cancel=cancel)
                        active[number] = (future, cancel)
                    dispatched_once = True
                    if once and not active:
                        break
                    stopped.wait(self.project.workflow.limits.poll_seconds)
            except BaseException:
                stopped.set()
                for _, cancel in active.values():
                    cancel.set()
                raise

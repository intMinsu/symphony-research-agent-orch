"""Small CLI surface; all writes and paid smoke tests are explicit commands."""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import threading
import tempfile
from collections import deque
from pathlib import Path

import yaml
from pydantic import ValidationError

from . import __version__
from .config import load_project, safe_read
from .engine import Orchestrator
from .errors import SRAError
from .models import ACTIVE, AgentEvent, Capabilities, Limits, RunRecord, Tracker, WorkSpec, Workflow, digest
from .runners import Runner, redact
from .specs import load_spec, render_spec
from .store import Store, default_state_dir
from .templates import AGENTS, CLAUDE, PLANNING, POLICY, workflow
from .workspace import git

SCHEMAS = {"workflow": Workflow, "workspec": WorkSpec, "agent-event": AgentEvent,
           "run-record": RunRecord, "capabilities": Capabilities}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="sra", description="Local-first research agent orchestration")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--project", type=Path, default=Path.cwd(), help="Research repository root")
    root.add_argument("--state-dir", type=Path, default=None, help="Private state directory (default: XDG state/sra)")
    sub = root.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create project policy templates without overwriting instructions")
    init.add_argument("--repository", required=True, help="GitHub owner/repository")
    sub.add_parser("validate", help="Validate WORKFLOW.md without executing configured commands")
    spec = sub.add_parser("spec", help="Validate or render a WorkSpec JSON file")
    specs = spec.add_subparsers(dest="spec_command", required=True)
    for name in ("validate", "render"):
        action = specs.add_parser(name)
        action.add_argument("path", type=Path)
        if name == "render":
            action.add_argument("--output", type=Path)
    schema = sub.add_parser("schema", help="Export JSON Schemas generated from the runtime models")
    schema.add_argument("kind", choices=[*SCHEMAS, "all"])
    schema.add_argument("--output-dir", type=Path)
    schema.add_argument("--check", action="store_true", help="Fail if checked-in schemas differ")

    def select(p):
        p.add_argument("--provider", choices=["codex", "claude"])
        p.add_argument("--model", help="Native model identifier; omitted means configured/CLI default")
        p.add_argument("--effort", help="Native effort value; never silently downgraded")

    def trust(p):
        p.add_argument("--trust-project", action="store_true", help="Acknowledge trusted local code/hooks/checks")

    doctor = sub.add_parser("doctor", help="Inspect CLI surfaces without version allowlists")
    select(doctor)
    trust(doctor)
    doctor.add_argument("--smoke", action="store_true", help="Actually call the model in a scratch repo; consumes quota")
    plan = sub.add_parser("plan", help="Generate a draft WorkSpec; never dispatch automatically")
    select(plan)
    trust(plan)
    plan.add_argument("--id", required=True)
    plan.add_argument("--request", required=True)
    plan.add_argument("--output", type=Path, required=True)
    publish = sub.add_parser("publish", help="Create/update the canonical spec Issue")
    publish.add_argument("path", type=Path)
    publish.add_argument("--ready", action="store_true", help="Also label and locally approve this exact spec/policy")
    inspect = sub.add_parser("inspect", help="Read the current remote WorkSpec and its approval digest")
    inspect.add_argument("issue", type=int)
    approve = sub.add_parser("approve", help="Approve a previously inspected remote spec and local policy")
    approve.add_argument("issue", type=int)
    approve.add_argument("--spec-sha", required=True)
    run = sub.add_parser("run", help="Execute one approved Issue in an isolated Git clone")
    run.add_argument("issue", type=int)
    select(run)
    trust(run)
    run.add_argument("--retry", action="store_true", help="Explicitly rework/retry; starts a fresh native session")
    run.add_argument("--feedback-file", type=Path, help="Reviewed PR feedback or research follow-up for this attempt")
    run.add_argument("--publish-pr", action="store_true", help="Commit/push and open a draft PR after verification")
    deliver = sub.add_parser("deliver", help="Publish the verified workspace as a draft PR")
    deliver.add_argument("issue", type=int)
    trust(deliver)
    watch = sub.add_parser("watch", help="Poll locally approved Issues; no automatic task retries")
    trust(watch)
    watch.add_argument("--once", action="store_true", help="Dispatch at most one concurrency-sized batch, then exit")
    watch.add_argument("--publish-pr", action="store_true")
    status = sub.add_parser("status", help="Inspect local runs; does not call a model")
    status.add_argument("--json", action="store_true")
    logs = sub.add_parser("logs", help="Show the last normalized, locally stored events")
    logs.add_argument("run_id")
    logs.add_argument("--lines", type=int, default=30)
    cancel = sub.add_parser("cancel", help="Request cancellation at the next supervision boundary")
    cancel.add_argument("run_id")
    recover = sub.add_parser("recover", help="Release an orphaned lease only after recorded PIDs are gone")
    recover.add_argument("run_id")
    recover.add_argument("--confirm-stopped", action="store_true")
    return root


def write_new(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        output.write(text)


def export_schemas(kind: str, output: Path | None = None, *, check=False) -> None:
    if kind == "all" and output is None:
        raise SRAError("schema all requires --output-dir")
    if check and output is None:
        raise SRAError("--check requires --output-dir")
    names = SCHEMAS if kind == "all" else [kind]
    for name in names:
        schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", **SCHEMAS[name].model_json_schema()}
        text = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if output:
            path = output / f"{name}.schema.json"
            if check:
                if not path.exists() or path.read_text() != text:
                    raise SRAError(f"Schema drift: regenerate {path}")
            else:
                output.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
        else:
            print(text, end="")


def dispatch(args) -> int:
    if args.command == "schema":
        export_schemas(args.kind, args.output_dir, check=args.check)
        return 0
    if args.command == "spec":
        spec = load_spec(args.path)
        if args.spec_command == "render":
            if args.output:
                write_new(args.output, render_spec(spec))
            else:
                print(render_spec(spec), end="")
        else:
            print(f"Valid WorkSpec {spec.id}, revision {spec.revision}, SHA-256 {digest(spec)}")
        return 0
    if args.command == "init":
        repository = Tracker(repository=args.repository).repository
        root = args.project.expanduser().resolve()
        if not (root / ".git").exists():
            raise SRAError("Initialize Git first; --project must be an existing repository root")
        if (root / "WORKFLOW.md").exists():
            raise SRAError("WORKFLOW.md already exists; refusing to overwrite project policy")
        files = {"WORKFLOW.md": workflow(repository), "AGENTS.md": AGENTS, "CLAUDE.md": CLAUDE,
                 ".agent/POLICY.md": POLICY, ".agent/PLANNING.md": PLANNING}
        for name, text in files.items():
            path = root / name
            if not path.resolve().is_relative_to(root):
                raise SRAError(f"Template path escapes project: {name}")
            if not path.exists():
                write_new(path, text)
                print(f"Created {name}")
            else:
                print(f"Preserved {name}")
        print("Configure checks, review policies, and commit the files to base_ref before execution.")
        return 0
    project = load_project(args.project)
    if args.command == "validate":
        print(f"Valid workflow for {project.workflow.tracker.repository}; policy SHA-256 {project.policy_sha}")
        return 0
    if args.command == "doctor":
        if not args.trust_project:
            raise SRAError("doctor executes configured binaries; pass --trust-project for a trusted configuration")
        providers = [args.provider] if args.provider else list(project.workflow.runners)
        failed = False
        for provider in providers:
            try:
                selection = project.selection(provider=provider, model=args.model, effort=args.effort)
                runner = Runner(selection, project.workflow.runners[provider], token_env=project.workflow.tracker.token_env)
                capabilities = runner.probe(project.root)
                if args.smoke:
                    with tempfile.TemporaryDirectory(prefix="sra-smoke-") as folder:
                        path = Path(folder)
                        git(path, "init")
                        result = runner.run("Reply with exactly OK. Do not edit files or run tools.", path,
                                            Limits(run_timeout_seconds=120, idle_timeout_seconds=60), read_only=True)
                        if result.text.strip() != "OK":
                            raise SRAError("Smoke returned a valid terminal event but did not answer OK")
                        capabilities = capabilities.model_copy(update={"evidence": "smoke"})
                print(capabilities.model_dump_json(indent=2))
                failed |= bool(capabilities.missing)
            except SRAError as exc:
                failed = True
                print(json.dumps({"provider": provider, "error": redact(str(exc))}))
        return int(failed)
    state_dir = (args.state_dir or default_state_dir()).expanduser().resolve()
    if state_dir.is_relative_to(project.root):
        raise SRAError("Runtime state must live outside the research repository")
    store = Store(state_dir)
    engine = Orchestrator(project, store)
    if args.command == "plan":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", args.id):
            raise SRAError("Invalid WorkSpec id")
        if args.output.exists():
            raise SRAError("Output already exists; choose a new draft filename")
        spec = engine.plan(args.request, args.id, trust_project=args.trust_project,
                           provider=args.provider, model=args.model, effort=args.effort)
        write_new(args.output, spec.model_dump_json(indent=2) + "\n")
        print(f"Draft written to {args.output}; inspect it before `publish --ready`.")
    elif args.command == "publish":
        issue = engine.publish(load_spec(args.path), ready=args.ready)
        print(issue["html_url"])
    elif args.command == "inspect":
        _, spec = engine._input(args.issue, require_ready=False)
        print(render_spec(spec))
        print("Locally approved:", store.approved(engine.repository, args.issue, spec, project.policy_sha))
    elif args.command == "approve":
        spec = engine.approve(args.issue, args.spec_sha)
        print(f"Approved Issue #{args.issue}: {digest(spec)}")
    elif args.command == "run":
        result = engine.run(args.issue, trust_project=args.trust_project, provider=args.provider,
                            model=args.model, effort=args.effort, retry=args.retry, publish_pr=args.publish_pr,
                            feedback=safe_read(args.feedback_file) if args.feedback_file else None)
        print(result.model_dump_json(indent=2))
    elif args.command == "deliver":
        print(engine.deliver(args.issue, trust_project=args.trust_project).model_dump_json(indent=2))
    elif args.command == "watch":
        engine.watch(trust_project=args.trust_project, once=args.once, publish_pr=args.publish_pr)
    elif args.command == "status":
        runs = store.list(engine.repository)
        if args.json:
            print(json.dumps([r.model_dump(mode="json") for r in runs], indent=2))
        else:
            print(f"{'ISSUE':<8} {'STATE':<17} {'PROVIDER':<8} {'TRY':<4} RUN")
            for run in runs:
                print(f"{run.issue_number:<8} {run.state:<17} {run.provider:<8} {run.attempt:<4} {run.id}")
    elif args.command == "logs":
        store.get(args.run_id)
        path = store.run_dir(args.run_id) / "events.jsonl"
        if not path.exists():
            print("No agent events yet; inspect status and local warnings.")
        else:
            with path.open() as log:
                for line in deque(log, maxlen=max(1, min(args.lines, 10000))):
                    print(line, end="")
    elif args.command == "cancel":
        record = store.get(args.run_id)
        if record.state not in ACTIVE:
            raise SRAError("Run is not active")
        (store.run_dir(args.run_id) / "cancel.request").write_text("cancel\n")
        print("Cancellation requested; the supervisor will stop its process group.")
    elif args.command == "recover":
        if not args.confirm_stopped:
            raise SRAError("Inspect the processes first, then use --confirm-stopped")
        print(store.recover(args.run_id).model_dump_json(indent=2))
    return 0


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    previous_handler = None
    if threading.current_thread() is threading.main_thread():
        previous_handler = signal.getsignal(signal.SIGTERM)
        def interrupted(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupted)
    try:
        return dispatch(args)
    except KeyboardInterrupt:
        print("Interrupted; supervised children are being stopped.", file=sys.stderr)
        return 130
    except (SRAError, ValidationError, OSError, yaml.YAMLError, ValueError) as exc:
        print("sra: " + redact(str(exc)).replace("\x1b", "\\x1b"), file=sys.stderr)
        return 2
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)

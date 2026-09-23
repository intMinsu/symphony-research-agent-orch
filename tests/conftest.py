from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from sra.config import load_project
from sra.models import WorkSpec
from sra.specs import issue_body
from sra.templates import AGENTS, CLAUDE, PLANNING, POLICY, WORKFLOW_BODY
from sra.workspace import git


@pytest.fixture
def spec():
    return WorkSpec.model_validate({
        "id": "EXP-001", "title": "Add a reproducible fixture", "objective": "Add a tested result fixture.",
        "scope": {"include": ["src/*", "reports/*"]},
        "acceptance": [{"id": "AC-1", "description": "Result fixture exists", "evidence": "Run unit check"}],
        "check_ids": ["unit"], "research": {"seeds": [42], "metrics": ["accuracy"]},
    })


@pytest.fixture
def fake_cli(tmp_path):
    path = tmp_path / "fake_cli.py"
    path.write_text(r"""
import json
import pathlib
import sys
import time

args = sys.argv[1:]
provider = args[args.index('--fake-provider') + 1]
mode = args[args.index('--fake-mode') + 1] if '--fake-mode' in args else 'ok'
if '--version' in args:
    print('unrecognized-future-build-9999.dev')
    raise SystemExit(0)
if '--help' in args:
    flags = '--json --sandbox --config --ask-for-approval --print --output-format --verbose --permission-mode --tools --allowedTools --strict-mcp-config --model --effort'
    if mode == 'missing':
        flags = flags.replace('--sandbox', '')
    print(flags)
    raise SystemExit(0)
prompt = sys.stdin.read()
if mode == 'fail':
    print('deliberate startup failure', file=sys.stderr)
    raise SystemExit(7)
if mode == 'hang':
    print('{}', flush=True)
    time.sleep(30)
if 'Return exactly one JSON object' in prompt:
    import re
    spec_id = re.search(r'Requested id: (\\S+)', prompt).group(1)
    text = json.dumps({'id': spec_id, 'title': 'Planned fixture', 'objective': 'Add a result fixture.',
                      'scope': {'include': ['src/*']},
                      'acceptance': [{'id': 'AC-1', 'description': 'Fixture exists', 'evidence': 'Inspect it'}],
                      'check_ids': ['unit']})
elif 'Reply with exactly OK' in prompt:
    text = 'OK'
else:
    pathlib.Path('src').mkdir(exist_ok=True)
    pathlib.Path('src/result.py').write_text('RESULT = 42\\n')
    if mode == 'scope':
        pathlib.Path('outside.txt').write_text('unauthorized')
    if mode == 'policy':
        pathlib.Path('WORKFLOW.md').write_text('overwritten')
    text = 'Implemented the fixture. Human acceptance review is still required.'
if provider == 'codex':
    print(json.dumps({'type':'thread.started','thread_id':'codex-session'}))
    print(json.dumps({'type':'future.optional.event','some_new_field':True}))
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':text}}))
    if mode != 'no-terminal':
        print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}}))
else:
    print(json.dumps({'type':'system','subtype':'init','session_id':'claude-session'}))
    print(json.dumps({'type':'future.optional.event','some_new_field':True}))
    if mode != 'no-terminal':
        print(json.dumps({'type':'result','subtype':'success','session_id':'claude-session','is_error':False,'result':text,'usage':{'input_tokens':8}}))
""".replace('\\\\', '\\'))
    return path


@pytest.fixture
def project(tmp_path, fake_cli):
    root = tmp_path / "research"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test Researcher")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "src").mkdir()
    (root / "src/base.py").write_text("BASE = True\n")
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    config = {
        "schema_version": 1, "tracker": {"repository": "example/research"},
        "planner": {"provider": "claude"}, "executor": {"provider": "codex"},
        "runners": {name: {"command": [sys.executable, str(fake_cli), "--fake-provider", name]}
                    for name in ("codex", "claude")},
        "checks": {"unit": {"argv": [sys.executable, "-c", "from src.result import RESULT; assert RESULT == 42"]}},
        "required_checks": ["unit"], "limits": {"run_timeout_seconds": 10, "idle_timeout_seconds": 5},
    }
    (root / "WORKFLOW.md").write_text("---\n" + yaml.safe_dump(config) + "---\n" + WORKFLOW_BODY)
    for name, text in {"AGENTS.md": AGENTS, "CLAUDE.md": CLAUDE, ".agent/POLICY.md": POLICY,
                       ".agent/PLANNING.md": PLANNING}.items():
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial research fixture")
    return load_project(root)


class FakeTracker:
    token = "fake-host-token-not-sent-to-child"

    def __init__(self, spec):
        self.item = {"number": 1, "state": "open", "title": spec.title, "body": issue_body(spec),
                     "html_url": "https://github.com/example/research/issues/1",
                     "labels": [{"name": "agent-ready"}], "user": {"login": "example"}}
        self.workpads = []
        self.pr_calls = []

    def issue(self, number):
        assert number == 1
        return dict(self.item)

    def eligible(self, issue):
        return issue["state"] == "open" and bool(issue.get("labels"))

    def candidates(self):
        return [self.item] if self.eligible(self.item) else []

    def publish(self, spec, ready=False):
        self.item["body"] = issue_body(spec)
        return self.item

    def workpad(self, number, text):
        self.workpads.append(text)

    def ensure_label(self):
        pass

    def request(self, method, path, data=None):
        return {}

    def pull_request(self, branch, base, title, body):
        self.pr_calls.append((branch, base, title, body))
        return {"html_url": "https://github.com/example/research/pull/2"}


@pytest.fixture
def tracker(spec):
    return FakeTracker(spec)

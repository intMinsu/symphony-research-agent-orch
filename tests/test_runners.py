import json
import os
import sys
import threading

import pytest

from sra.errors import CompatibilityError, ProcessError
from sra.models import Limits, RunnerConfig, Selection
from sra.process import execute
from sra.runners import EventParser, Runner, child_env, redact


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_unknown_version_is_allowed(tmp_path, fake_cli, provider):
    runner = Runner(Selection(provider=provider), RunnerConfig(command=[sys.executable, str(fake_cli),
                                                                       "--fake-provider", provider]))
    profile = runner.probe(tmp_path)
    assert profile.version == "unrecognized-future-build-9999.dev"
    assert not profile.missing
    assert profile.evidence == "help"
    result = runner.run("Reply with exactly OK", tmp_path, Limits(), read_only=True)
    assert result.text == "OK"
    assert result.session_id
    assert not any("dangerously" in arg or "bypassPermissions" in arg for arg in runner.argv())


def test_security_feature_is_not_silently_dropped(tmp_path, fake_cli):
    runner = Runner(Selection(), RunnerConfig(command=[sys.executable, str(fake_cli), "--fake-provider", "codex",
                                                       "--fake-mode", "missing"]))
    assert "--sandbox" in runner.probe(tmp_path).missing
    with pytest.raises(CompatibilityError):
        runner.argv()


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_missing_terminal_event_is_failure(tmp_path, fake_cli, provider):
    runner = Runner(Selection(provider=provider), RunnerConfig(command=[sys.executable, str(fake_cli),
                    "--fake-provider", provider, "--fake-mode", "no-terminal"]))
    with pytest.raises(CompatibilityError, match="terminal"):
        runner.run("Reply with exactly OK", tmp_path, Limits())


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_unknown_optional_event_is_observable(provider):
    parser = EventParser(provider)
    events = parser.feed("stdout", '{"type":"brand.new.event","new":{"x":1}}')
    assert events[0].type == "diagnostic"
    assert not parser.failed and not parser.completed


def test_claude_error_result_is_failure():
    parser = EventParser("claude")
    parser.feed("stdout", json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": False}))
    assert parser.failed and not parser.completed


def test_codex_turn_failure():
    parser = EventParser("codex")
    parser.feed("stdout", '{"type":"turn.failed","error":{"message":"denied"}}')
    assert parser.failed


def test_environment_redacts_and_strips_tracker_secrets(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "super-secret-github-token")
    monkeypatch.setenv("GH_TOKEN", "another-github-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-key")
    monkeypatch.setenv("UNRELATED_SECRET", "unrelated-test-secret")
    env = child_env("claude", RunnerConfig(command=["claude"]))
    assert "GITHUB_TOKEN" not in env and "GH_TOKEN" not in env and "UNRELATED_SECRET" not in env
    assert env["ANTHROPIC_API_KEY"] == "anthropic-test-key"
    assert "super-secret-github-token" not in redact("Error super-secret-github-token")


def test_process_stdin_and_streaming(tmp_path):
    lines = []
    result = execute([sys.executable, "-c", "import sys; print(sys.stdin.read()); print('err',file=sys.stderr)"],
                     cwd=tmp_path, stdin="a" * 100000, on_line=lambda stream, line: lines.append((stream, line)))
    assert result.returncode == 0 and len(result.stdout.strip()) == 100000
    assert any(stream == "stderr" for stream, _ in lines)


def test_process_timeout(tmp_path):
    with pytest.raises(ProcessError, match="timeout"):
        execute([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path, timeout=0.1)


def test_process_output_limit(tmp_path):
    with pytest.raises(ProcessError, match="byte limit"):
        execute([sys.executable, "-c", "print('x'*100000)"], cwd=tmp_path, max_bytes=1000)


def test_process_cancellation(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ProcessError, match="cancelled"):
        execute([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path, cancel=cancel)

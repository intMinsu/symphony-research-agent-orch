"""CLI-specific code lives here; orchestration never consumes native events."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable

from .errors import CompatibilityError, ProcessError
from .models import AgentEvent, Capabilities, Limits, Provider, RunnerConfig, Selection
from .process import execute

BASE_ENV = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "TMPDIR", "XDG_CONFIG_HOME"}
PROVIDER_ENV = {
    "codex": {"OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_HOME"},
    "claude": {"ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR"},
}


def child_env(provider: Provider, config: RunnerConfig, token_env="GITHUB_TOKEN") -> dict[str, str]:
    allowed = BASE_ENV | PROVIDER_ENV[provider] | set(config.env_passthrough)
    allowed -= {"GITHUB_TOKEN", "GH_TOKEN", token_env}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update({"NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0"})
    return env


def redact(text: str) -> str:
    for key, value in os.environ.items():
        if len(value) >= 8 and any(part in key.upper() for part in ("TOKEN", "SECRET", "API_KEY", "PASSWORD")):
            text = text.replace(value, "[REDACTED]")
    return text


@dataclass(frozen=True)
class AgentResult:
    text: str
    session_id: str | None
    usage: dict
    capabilities: Capabilities


class Runner:
    def __init__(self, selection: Selection, config: RunnerConfig, *, token_env="GITHUB_TOKEN"):
        self.selection = selection
        self.config = config
        self.env = child_env(selection.provider, config, token_env)
        self.capabilities: Capabilities | None = None

    def probe(self, cwd: Path) -> Capabilities:
        command = self.config.command
        version_result = execute(command + ["--version"], cwd=cwd, env=self.env, timeout=15, max_bytes=262144)
        version = (version_result.stdout.strip() or "unknown")[:300]
        help_result = execute(command + ["--help"], cwd=cwd, env=self.env, timeout=15, max_bytes=524288)
        help_text = help_result.stdout + help_result.stderr
        if self.selection.provider == "codex":
            extra = execute(command + ["exec", "--help"], cwd=cwd, env=self.env,
                            timeout=15, max_bytes=524288)
            help_text += extra.stdout + extra.stderr
            required = {"--json", "--sandbox", "--ask-for-approval", "--config"}
            if self.selection.effort is not None or self.config.codex_network_access:
                required.add("--config")
        else:
            required = {"--print", "--output-format", "--verbose", "--permission-mode",
                        "--tools", "--allowedTools", "--strict-mcp-config"}
            if self.selection.effort is not None:
                required.add("--effort")
        if self.selection.model is not None:
            required.add("--model")
        flags = set(re.findall(r"--[A-Za-z][A-Za-z0-9-]*", help_text))
        if "--allowed-tools" in flags:
            flags.add("--allowedTools")
        if re.search(r"(?:^|[\s,])-p(?:[\s,]|$)", help_text):
            flags.add("--print")
        missing = sorted(required - flags)
        self.capabilities = Capabilities(provider=self.selection.provider, command=command, version=version,
                                         flags=sorted(flags), missing=missing)
        return self.capabilities

    def argv(self, *, read_only=False) -> list[str]:
        if not self.capabilities or self.capabilities.missing:
            missing = self.capabilities.missing if self.capabilities else ["probe"]
            raise CompatibilityError(f"Missing required CLI surfaces: {', '.join(missing)}")
        command = list(self.config.command)
        if self.selection.provider == "codex":
            command += ["--ask-for-approval", "never", "exec", "--json", "--sandbox",
                        "read-only" if read_only else "workspace-write"]
            if self.selection.effort is not None:
                command += ["--config", "model_reasoning_effort=" + json.dumps(self.selection.effort)]
            if not read_only and "--config" in self.capabilities.flags:
                access = "true" if self.config.codex_network_access else "false"
                command += ["--config", "sandbox_workspace_write.network_access=" + access]
        else:
            tools = ["Read", "Glob", "Grep"] if read_only else self.config.claude_tools
            command += ["--print", "--verbose", "--output-format", "stream-json",
                        "--permission-mode", "dontAsk", "--tools", ",".join(tools),
                        ("--allowed-tools" if "--allowed-tools" in self.capabilities.flags else "--allowedTools"),
                        ",".join(tools), "--strict-mcp-config"]
            if self.selection.effort is not None:
                command += ["--effort", self.selection.effort]
        if self.selection.model is not None:
            command += ["--model", self.selection.model]
        if self.selection.provider == "codex":
            command += ["-"]
        return command

    def run(self, prompt: str, cwd: Path, limits: Limits, *, read_only=False,
            cancel: Event | None = None, on_event: Callable[[AgentEvent], None] | None = None,
            on_start: Callable[[int], None] | None = None, heartbeat=None) -> AgentResult:
        if not self.capabilities:
            self.probe(cwd)
        parser = EventParser(self.selection.provider)

        def consume(stream: str, line: str) -> None:
            # Unknown fields/events are tolerated; missing completion is not.
            events = parser.feed(stream, redact(line))
            for event in events:
                if on_event:
                    on_event(event)

        result = execute(self.argv(read_only=read_only), cwd=cwd, env=self.env, stdin=prompt,
                         timeout=limits.run_timeout_seconds, idle_timeout=limits.idle_timeout_seconds,
                         max_bytes=limits.max_output_bytes, cancel=cancel, on_line=consume,
                         on_start=on_start, heartbeat=heartbeat)
        if result.returncode != 0:
            raise ProcessError(f"{self.selection.provider} exited {result.returncode}: "
                               + redact(result.stderr[-2000:]))
        if parser.failed:
            raise ProcessError(f"Agent reported failure: {parser.failure_text[:2000]}")
        if not parser.completed:
            raise CompatibilityError("Exit 0 without a recognized successful terminal event; not treating as done")
        return AgentResult(parser.text, parser.session_id, parser.usage, self.capabilities)


class EventParser:
    def __init__(self, provider: Provider):
        self.provider = provider
        self.session_id = None
        self.text = ""
        self.usage = {}
        self.completed = False
        self.failed = False
        self.failure_text = ""

    def feed(self, stream: str, line: str) -> list[AgentEvent]:
        if not line.strip():
            return []
        if stream == "stderr":
            return [self.event("diagnostic", text=line[:4000])]
        try:
            value = json.loads(line)
        except ValueError:
            return [self.event("diagnostic", text=line[:4000], native_type="non-json")]
        if not isinstance(value, dict):
            return [self.event("diagnostic", native_type="non-object")]
        native = value.get("type", "unknown")
        if self.provider == "codex":
            if native == "thread.started":
                self.session_id = value.get("thread_id")
                return [self.event("session.started", native_type=native)]
            if native == "turn.completed":
                self.completed = True
                self.usage = value.get("usage") or {}
                return [self.event("usage", data=self.usage), self.event("completed", native_type=native)]
            if native == "turn.failed":
                self.failed = True
                self.failure_text = json.dumps(value.get("error", value))
                return [self.event("failed", text=self.failure_text, native_type=native)]
            if native.startswith("item."):
                item = value.get("item") or {}
                if not isinstance(item, dict):
                    return [self.event("diagnostic", native_type=native)]
                if item.get("type") == "agent_message" and native == "item.completed":
                    self.text = str(item.get("text", ""))
                    return [self.event("progress", text=self.text, native_type=native)]
                return [self.event("tool", native_type=native, data={"item_type": item.get("type"),
                                                                   "status": item.get("status")})]
        else:
            if native == "system" and value.get("subtype") == "init":
                self.session_id = value.get("session_id")
                return [self.event("session.started", native_type="system.init")]
            if native == "assistant":
                content = (value.get("message") or {}).get("content", [])
                text = "\n".join(str(x.get("text", "")) for x in content
                                 if isinstance(x, dict) and x.get("type") == "text")
                if text:
                    self.text = text
                return [self.event("progress", text=text or None, native_type=native)]
            if native == "result":
                self.session_id = value.get("session_id", self.session_id)
                self.failed = bool(value.get("is_error")) or value.get("subtype") != "success"
                self.completed = not self.failed
                self.text = str(value.get("result", self.text))
                self.failure_text = json.dumps(value.get("errors", self.text)) if self.failed else ""
                self.usage = value.get("usage") or {}
                return [self.event("usage", data=self.usage),
                        self.event("failed" if self.failed else "completed", native_type=native,
                                   text=self.failure_text if self.failed else self.text)]
        return [self.event("diagnostic", native_type=str(native), data={"unmapped": True})]

    def event(self, event_type, **fields) -> AgentEvent:
        return AgentEvent(type=event_type, provider=self.provider, session_id=self.session_id, **fields)

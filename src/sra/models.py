"""The versioned public contracts. Generate JSON Schemas with `sra schema`."""
from __future__ import annotations

import hashlib
import json
import re
import socket
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Provider = Literal["codex", "claude"]
NonEmpty = Annotated[str, Field(min_length=1, max_length=20000)]
Key = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def relative_path(value: str) -> str:
    p = PurePosixPath(value)
    if not value or p.is_absolute() or ".." in p.parts or "\\" in value or "\x00" in value:
        raise ValueError("Expected a non-empty, repository-relative POSIX path without '..'")
    if p.parts and p.parts[0] == ".git":
        raise ValueError(".git paths are reserved")
    return value


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True, frozen=True)


class Selection(Contract):
    provider: Provider = "codex"
    model: str | None = Field(default=None, min_length=1, max_length=200)
    effort: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("model", "effort")
    @classmethod
    def identifier(cls, value: str | None) -> str | None:
        if value is not None and (value.startswith("-") or any(c.isspace() for c in value)):
            raise ValueError("Model/effort must be a single identifier, not CLI arguments")
        return value


class RunnerConfig(Contract):
    command: list[str] = Field(min_length=1, max_length=16)
    # CLI defaults are preserved when model/effort are absent. No invented model IDs.
    model: str | None = None
    effort: str | None = None
    codex_network_access: bool = False
    claude_tools: list[Literal["Read", "Glob", "Grep", "Edit", "Write", "Bash"]] = Field(
        default_factory=lambda: ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]
    )
    env_passthrough: list[str] = Field(default_factory=list)

    @field_validator("command")
    @classmethod
    def valid_command(cls, value: list[str]) -> list[str]:
        if any(not s or "\x00" in s or "\n" in s for s in value):
            raise ValueError("command is an argv array, not a shell command")
        return value

    @field_validator("env_passthrough")
    @classmethod
    def env_names(cls, values: list[str]) -> list[str]:
        for value in values:
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
                raise ValueError("Invalid environment variable name")
            if value in {"GH_TOKEN", "GITHUB_TOKEN"}:
                raise ValueError("Tracker tokens cannot be passed to agents")
        return values

    @model_validator(mode="after")
    def validate_ids(self) -> RunnerConfig:
        Selection(model=self.model, effort=self.effort)
        return self


class Check(Contract):
    argv: list[str] = Field(min_length=1)
    timeout_seconds: int = Field(default=300, ge=1, le=86400)

    @field_validator("argv")
    @classmethod
    def command(cls, value: list[str]) -> list[str]:
        if any(not v or "\x00" in v for v in value):
            raise ValueError("Check commands must be non-empty argv arrays")
        return value


class Limits(Contract):
    run_timeout_seconds: int = Field(default=3600, ge=1, le=86400)
    idle_timeout_seconds: int = Field(default=600, ge=1, le=86400)
    max_output_bytes: int = Field(default=16_777_216, ge=4096, le=268_435_456)
    max_attempts: int = Field(default=3, ge=1, le=20)
    max_concurrent_runs: int = Field(default=1, ge=1, le=8)
    poll_seconds: int = Field(default=30, ge=5, le=3600)


class Tracker(Contract):
    repository: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    token_env: str = "GITHUB_TOKEN"
    ready_label: str = "agent-ready"
    allowed_authors: list[str] = Field(default_factory=list)

    @field_validator("token_env")
    @classmethod
    def token_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
            raise ValueError("Invalid token environment variable")
        if value in {"OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY"}:
            raise ValueError("Use a distinct GitHub credential")
        return value


class Workflow(Contract):
    schema_version: Literal[1] = 1
    tracker: Tracker
    base_ref: str = "main"
    planner: Selection = Field(default_factory=lambda: Selection(provider="claude"))
    executor: Selection = Field(default_factory=Selection)
    runners: dict[Provider, RunnerConfig] = Field(default_factory=lambda: {
        "codex": RunnerConfig(command=["codex"]),
        "claude": RunnerConfig(command=["claude"]),
    })
    checks: dict[Key, Check] = Field(default_factory=dict)
    required_checks: list[Key] = Field(default_factory=list)
    limits: Limits = Field(default_factory=Limits)
    # These are pre-publication guards, NOT filesystem sandbox rules.
    protected_paths: list[str] = Field(default_factory=lambda: [
        "WORKFLOW.md", "AGENTS.md", "CLAUDE.md", ".agent/*", ".github/*",
        ".env", ".env.*", "*.pem", "*.key",
    ])
    max_changed_file_bytes: int = Field(default=2_000_000, ge=1, le=100_000_000)
    git_author_name: str = "Research Agent"
    git_author_email: str = "research-agent@localhost"

    @field_validator("base_ref")
    @classmethod
    def ref(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_.-]*", value) or ".." in value:
            raise ValueError("Invalid base_ref")
        return value

    @model_validator(mode="after")
    def consistency(self) -> Workflow:
        missing = set(self.required_checks) - self.checks.keys()
        if missing:
            raise ValueError(f"Undefined required checks: {sorted(missing)}")
        if self.planner.provider not in self.runners or self.executor.provider not in self.runners:
            raise ValueError("Planner and executor must reference configured runners")
        for runner in self.runners.values():
            if self.tracker.token_env in runner.env_passthrough:
                raise ValueError("Tracker token must remain on the host")
        return self


class Criterion(Contract):
    id: Key
    description: NonEmpty
    evidence: NonEmpty


class Scope(Contract):
    include: list[str] = Field(min_length=1)
    exclude: list[str] = Field(default_factory=list)

    @field_validator("include", "exclude")
    @classmethod
    def patterns(cls, values: list[str]) -> list[str]:
        return [relative_path(v) for v in values]


class Research(Contract):
    hypothesis: str | None = None
    datasets: list[str] = Field(default_factory=list)
    seeds: list[int] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list)

    @field_validator("artifact_paths")
    @classmethod
    def paths(cls, values: list[str]) -> list[str]:
        return [relative_path(v) for v in values]


class WorkSpec(Contract):
    schema_version: Literal[1] = 1
    id: Key
    revision: int = Field(default=1, ge=1)
    title: Annotated[str, Field(min_length=1, max_length=180)]
    kind: Literal["implementation", "experiment", "investigation"] = "implementation"
    objective: NonEmpty
    scope: Scope
    acceptance: list[Criterion] = Field(min_length=1)
    check_ids: list[Key] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    research: Research = Field(default_factory=Research)
    execution: Selection | None = None
    delivery: Literal["draft_pr", "report"] = "draft_pr"

    @model_validator(mode="after")
    def unique_criteria(self) -> WorkSpec:
        ids = [x.id for x in self.acceptance]
        if len(ids) != len(set(ids)):
            raise ValueError("Acceptance criterion IDs must be unique")
        return self


EventType = Literal["session.started", "progress", "tool", "usage", "completed", "failed", "diagnostic"]


class AgentEvent(Contract):
    schema_version: Literal[1] = 1
    type: EventType
    timestamp: str = Field(default_factory=utcnow)
    provider: Provider
    native_type: str | None = None
    session_id: str | None = None
    text: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class Capabilities(Contract):
    provider: Provider
    command: list[str]
    version: str = "unknown"
    flags: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    evidence: Literal["help", "smoke"] = "help"
    # Help is surface evidence only, not proof of authentication/model/sandbox behavior.


State = Literal["preparing", "running", "verifying", "delivering", "awaiting_review", "failed", "blocked", "cancelled"]
ACTIVE = ("preparing", "running", "verifying", "delivering")


class CheckResult(Contract):
    id: Key
    returncode: int


class RunEvidence(Contract):
    checks: list[CheckResult] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    fingerprint: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    cli_version: str | None = None
    verified_at: str | None = None
    human_acceptance_required: Literal[True] = True
    head_sha: str | None = None


class RunRecord(Contract):
    schema_version: Literal[1] = 1
    id: str
    repository: str
    issue_number: int
    spec_sha256: str
    policy_sha256: str
    state: State
    attempt: int
    provider: Provider
    model: str | None = None
    effort: str | None = None
    workspace: str
    branch: str
    base_sha: str
    session_id: str | None = None
    host: str = Field(default_factory=socket.gethostname)
    owner_pid: int
    child_pid: int | None = None
    created_at: str
    updated_at: str
    error: str | None = None
    evidence: RunEvidence = Field(default_factory=RunEvidence)
    pr_url: str | None = None

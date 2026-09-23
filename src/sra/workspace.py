"""Separate Git clones preserve source checkouts and do not share mutable Git metadata."""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .config import Project, safe_read
from .errors import ConflictError, SRAError
from .models import WorkSpec, digest
from .process import execute
from .specs import render_spec


def git(cwd: Path, *args: str, env=None, timeout=120) -> str:
    environment = dict(os.environ) if env is None else dict(env)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_LITERAL_PATHSPECS"] = "1"
    result = execute(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                      "-c", "commit.gpgsign=false", *args], cwd=cwd, env=environment, timeout=timeout)
    if result.returncode:
        raise SRAError(f"Git {args[0]} failed: {result.stderr[-1500:]}")
    return result.stdout.strip() if "-z" not in args else result.stdout


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


@dataclass(frozen=True)
class WorkspaceInfo:
    path: Path
    branch: str
    base_sha: str
    manifest: Path


class Workspaces:
    def __init__(self, project: Project, state_dir: Path):
        self.project = project
        self.root = state_dir / "workspaces" / digest(project.workflow.tracker.repository)[:16]
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def describe(self, issue: int, spec: WorkSpec) -> WorkspaceInfo:
        folder = self.root / str(issue)
        manifest = self.root / f"{issue}.json"
        branch = f"sra/issue-{issue}"
        if manifest.exists():
            data = json.loads(safe_read(manifest))
            if data["source"] != str(self.project.root) or data["spec_id"] != spec.id:
                raise ConflictError("Workspace belongs to a different source checkout or WorkSpec")
            return WorkspaceInfo(folder, branch, data["base_sha"], manifest)
        base = git(self.project.root, "rev-parse", "--verify", self.project.workflow.base_ref + "^{commit}")
        return WorkspaceInfo(folder, branch, base, manifest)

    def prepare(self, info: WorkspaceInfo, spec: WorkSpec) -> None:
        source = self.project.root
        if git(source, "rev-parse", "--show-toplevel") != str(source):
            raise SRAError("--project must point to the repository root")
        if git(source, "status", "--porcelain"):
            raise SRAError("Source checkout is dirty. Commit/stash changes; SRA executes committed input only")
        if info.path.is_symlink() or not info.path.resolve().is_relative_to(self.root.resolve()):
            raise SRAError("Workspace path escapes the configured root")
        if not info.manifest.exists():
            if info.path.exists():
                raise ConflictError("Incomplete workspace exists; inspect it before manually removing/recovering it")
            git(self.root, "clone", "--no-local", "--no-hardlinks", "--no-checkout", str(source), str(info.path))
            git(info.path, "checkout", "-b", info.branch, info.base_sha)
            info.manifest.write_text(json.dumps({"source": str(source), "spec_id": spec.id,
                                                "base_sha": info.base_sha}, indent=2) + "\n")
        self.assert_identity(info)
        # Do not expose a write-capable origin as part of the normal agent task.
        if "origin" in git(info.path, "remote").splitlines():
            git(info.path, "remote", "remove", "origin")
        for name in ("WORKFLOW.md", "AGENTS.md", "CLAUDE.md", ".agent/POLICY.md", ".agent/PLANNING.md"):
            original = source / name
            copied = info.path / name
            if original.exists() != copied.exists():
                raise SRAError(f"Policy {name} differs from base_ref; commit it to the selected base first")
            if original.exists() and safe_read(original) != safe_read(copied):
                raise SRAError(f"Workspace policy differs: {name}; explicit rebase/setup required")
        spec_path = info.path / f"docs/specs/{spec.id}.md"
        if not spec_path.resolve().is_relative_to(info.path.resolve()):
            raise SRAError("Spec path escapes workspace")
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(render_spec(spec), encoding="utf-8")

    def assert_identity(self, info: WorkspaceInfo) -> None:
        if git(info.path, "branch", "--show-current") != info.branch:
            raise ConflictError("Agent/user switched the managed branch; inspect the workspace")
        git(info.path, "merge-base", "--is-ancestor", info.base_sha, "HEAD")

    def changed(self, info: WorkspaceInfo) -> list[str]:
        tracked = git(info.path, "diff", "--name-only", "--no-renames", "-z", info.base_sha, "--")
        untracked = git(info.path, "ls-files", "--others", "--exclude-standard", "-z")
        return sorted(set(x for x in (tracked + untracked).split("\x00") if x))

    def fingerprint(self, info: WorkspaceInfo, spec: WorkSpec) -> tuple[list[str], str]:
        self.assert_identity(info)
        paths = self.changed(info)
        generated = f"docs/specs/{spec.id}.md"
        if not (info.path / generated).is_file() or safe_read(info.path / generated) != render_spec(spec):
            raise SRAError("The approved generated spec was changed or removed")
        hashes = {}
        for name in paths:
            path = info.path / name
            if name != generated:
                if matches(name, self.project.workflow.protected_paths):
                    raise SRAError(f"Protected path changed: {name}")
                if not matches(name, spec.scope.include) or matches(name, spec.scope.exclude):
                    raise SRAError(f"Change outside the approved scope: {name}")
            if path.is_symlink() or not path.resolve().is_relative_to(info.path.resolve()):
                raise SRAError(f"Changed symlink/escaping path is not supported: {name}")
            if path.exists():
                if not path.is_file() or path.stat().st_size > self.project.workflow.max_changed_file_bytes:
                    raise SRAError(f"Changed path is a directory/submodule or exceeds the file limit: {name}")
                hashes[name] = [path.stat().st_mode & 0o777, hashlib.sha256(path.read_bytes()).hexdigest()]
            else:
                hashes[name] = None
        return paths, digest({"base_sha": info.base_sha, "files": hashes})

    def commit(self, info: WorkspaceInfo, spec: WorkSpec, expected_fingerprint: str) -> str:
        paths, fingerprint = self.fingerprint(info, spec)
        if fingerprint != expected_fingerprint:
            raise ConflictError("Workspace changed after verification; re-run before delivery")
        if paths:
            git(info.path, "add", "--", *paths)
        if git(info.path, "status", "--porcelain"):
            git(info.path, "-c", "user.name=" + self.project.workflow.git_author_name,
                "-c", "user.email=" + self.project.workflow.git_author_email,
                "commit", "-m", f"research: {spec.title} ({spec.id})")
        if git(info.path, "status", "--porcelain"):
            raise ConflictError("Workspace changed during commit; revalidate before publishing")
        _, committed_fingerprint = self.fingerprint(info, spec)
        if committed_fingerprint != expected_fingerprint:
            raise ConflictError("Committed content differs from verified content; do not publish")
        return git(info.path, "rev-parse", "HEAD")

    def push(self, info: WorkspaceInfo, token: str, head_sha: str) -> None:
        repo = self.project.workflow.tracker.repository
        # Credentials are transient host-process environment values, not argv or persisted Git config.
        env = dict(os.environ)
        auth = base64.b64encode(("x-access-token:" + token).encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + auth,
                    "GIT_CONFIG_KEY_1": "credential.helper", "GIT_CONFIG_VALUE_1": ""})
        git(info.path, "push", "https://github.com/" + repo + ".git",
            head_sha + ":refs/heads/" + info.branch, env=env)

"""Load a trusted, local WORKFLOW.md; the GitHub issue cannot define commands."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import SRAError
from .models import Selection, WorkSpec, Workflow, digest


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise SRAError(f"Duplicate or non-string YAML key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def safe_read(path: Path, limit: int = 262144) -> str:
    if path.stat().st_size > limit:
        raise SRAError(f"File exceeds {limit} bytes: {path}")
    return path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class Project:
    root: Path
    workflow: Workflow
    instructions: str
    policy_sha: str

    def validate_spec(self, spec: WorkSpec) -> None:
        missing = set(spec.check_ids) - self.workflow.checks.keys()
        if missing:
            raise SRAError(f"Spec requests undefined checks: {sorted(missing)}")
        if spec.execution and spec.execution.provider not in self.workflow.runners:
            raise SRAError("Spec selects an unconfigured runner")

    def selection(self, spec: WorkSpec | None = None, *, planner=False,
                  provider=None, model=None, effort=None) -> Selection:
        selected = self.workflow.planner if planner else (spec.execution if spec else None)
        selected = selected or self.workflow.executor
        chosen = provider or selected.provider
        if chosen not in self.workflow.runners:
            raise SRAError(f"Runner not configured: {chosen}")
        config = self.workflow.runners[chosen]
        # Never forward one provider's model name to another provider.
        same = chosen == selected.provider
        return Selection(provider=chosen,
                         model=model or (selected.model if same else None) or config.model,
                         effort=effort or (selected.effort if same else None) or config.effort)


def load_project(root: Path) -> Project:
    root = root.expanduser().resolve()
    path = root / "WORKFLOW.md"
    if not path.exists() or not path.resolve().is_relative_to(root):
        raise SRAError("Expected a local WORKFLOW.md. Run `sra init` first.")
    text = safe_read(path)
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise SRAError("WORKFLOW.md must start with YAML front matter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise SRAError("WORKFLOW.md front matter has no closing ---") from exc
    config = Workflow.model_validate(yaml.load("\n".join(lines[1:end]), Loader=UniqueLoader))
    instructions = "\n".join(lines[end+1:]).strip()
    policy = {"workflow": config.model_dump(mode="json"), "instructions": instructions}
    for name in ("AGENTS.md", "CLAUDE.md", ".agent/POLICY.md", ".agent/PLANNING.md"):
        p = root / name
        if p.exists():
            if not p.resolve().is_relative_to(root):
                raise SRAError(f"Instruction file escapes the project: {name}")
            policy[name] = safe_read(p)
    return Project(root, config, instructions, digest(policy))

"""Canonical Issue payload + human-readable Markdown from the same WorkSpec."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import safe_read
from .errors import SRAError
from .models import WorkSpec, digest

MARKER = "<!-- sra:workspec:v1 -->"
END = "<!-- /sra:workspec -->"


def load_spec(path: Path) -> WorkSpec:
    return WorkSpec.model_validate_json(safe_read(path))


def render_spec(spec: WorkSpec) -> str:
    lines = [f"# {spec.id}: {spec.title}", "", f"Revision: {spec.revision}",
             f"Spec SHA-256: `{digest(spec)}`", f"Kind: {spec.kind}", "",
             "## Objective", "", spec.objective, "", "## Scope", "",
             "Include: " + ", ".join(f"`{p}`" for p in spec.scope.include),
             "Exclude: " + (", ".join(f"`{p}`" for p in spec.scope.exclude) or "None"),
             "", "## Acceptance and evidence", ""]
    for criterion in spec.acceptance:
        lines.extend([f"- [ ] **{criterion.id}**: {criterion.description}", f"  Evidence: {criterion.evidence}"])
    lines += ["", "## Required check IDs", "", ", ".join(spec.check_ids) or "Project defaults only",
              "", "## Non-goals", ""]
    lines += [f"- {x}" for x in spec.non_goals] or ["None specified"]
    lines += ["", "## Stop conditions", ""]
    lines += [f"- {x}" for x in spec.stop_conditions] or ["Stop for access, safety, or scope blockers."]
    lines += ["", "## Research metadata", "", "```json", spec.research.model_dump_json(indent=2),
              "```", "", "## Delivery", "", spec.delivery, ""]
    return "\n".join(lines)


def issue_body(spec: WorkSpec) -> str:
    body = render_spec(spec) + "\n<details>\n<summary>Canonical machine-readable WorkSpec</summary>\n\n" + MARKER
    body += "\n```json\n" + spec.model_dump_json(indent=2) + "\n```\n" + END + "\n</details>\n"
    if len(body.encode()) > 60000:
        raise SRAError("WorkSpec is too large for the Issue transport (60 KB limit)")
    return body


def parse_issue(body: str) -> WorkSpec:
    if len(body.encode()) > 65536 or body.count(MARKER) != 1 or body.count(END) != 1:
        raise SRAError("Issue must contain exactly one SRA WorkSpec block")
    block = body.split(MARKER, 1)[1].split(END, 1)[0].strip()
    match = re.fullmatch(r"```json\s*\n(.*?)\n```", block, re.S)
    if not match:
        raise SRAError("Invalid SRA WorkSpec block")
    return WorkSpec.model_validate_json(match.group(1))


def planner_result(text: str) -> WorkSpec:
    text = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, re.S)
    if match:
        text = match.group(1)
    try:
        return WorkSpec.model_validate(json.loads(text))
    except (ValueError, TypeError) as exc:
        raise SRAError("Planner did not return a valid WorkSpec JSON object; inspect the local run log") from exc

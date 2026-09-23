import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from sra.cli import SCHEMAS, export_schemas, main
from sra.config import Project, load_project
from sra.errors import SRAError
from sra.models import RunnerConfig, Selection, WorkSpec, Workflow, digest
from sra.specs import issue_body, parse_issue, planner_result, render_spec


def test_issue_roundtrip(spec):
    assert parse_issue(issue_body(spec)) == spec
    assert digest(spec) in render_spec(spec)


@pytest.mark.parametrize("field,value", [("schema_version", 2), ("revision", 0), ("id", "../escape"),
                                         ("command", "rm -rf /"), ("delivery", "merge")])
def test_invalid_spec(spec, field, value):
    with pytest.raises(ValidationError):
        WorkSpec.model_validate({**spec.model_dump(), field: value})


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "src/../../secret", ".git/config", "x\\y"])
def test_scope_paths(spec, path):
    data = spec.model_dump()
    data["scope"]["include"] = [path]
    with pytest.raises(ValidationError):
        WorkSpec.model_validate(data)


def test_duplicate_criterion(spec):
    with pytest.raises(ValidationError):
        WorkSpec.model_validate({**spec.model_dump(), "acceptance": spec.model_dump()["acceptance"] * 2})


def test_duplicate_issue_blocks(spec):
    with pytest.raises(SRAError):
        parse_issue(issue_body(spec) * 2)


def test_planner_json_fence(spec):
    assert planner_result("```json\n" + spec.model_dump_json() + "\n```") == spec
    with pytest.raises(SRAError):
        planner_result("I think this is a good plan")


def test_duplicate_yaml_key(project):
    p = project.root / "WORKFLOW.md"
    p.write_text("---\nschema_version: 1\nschema_version: 2\n---\n")
    with pytest.raises(SRAError, match="Duplicate"):
        load_project(project.root)


def test_undefined_check(project, spec):
    bad = spec.model_copy(update={"check_ids": ["shell-from-issue"]})
    with pytest.raises(SRAError, match="undefined"):
        project.validate_spec(bad)


def test_model_names_are_not_globally_allowlisted():
    assert Selection(model="future-model", effort="future-effort").model == "future-model"
    with pytest.raises(ValidationError):
        Selection(model="--dangerous")


def test_provider_switch_does_not_leak_model(project):
    config = project.workflow.model_copy(update={"executor": Selection(provider="codex", model="codex-only")})
    changed = Project(project.root, config, project.instructions, project.policy_sha)
    assert changed.selection(provider="claude").model is None


def test_token_passthrough_denied():
    with pytest.raises(ValidationError):
        RunnerConfig(command=["codex"], env_passthrough=["GITHUB_TOKEN"])


def test_required_check_exists():
    with pytest.raises(ValidationError):
        Workflow.model_validate({"tracker": {"repository": "example/research"}, "required_checks": ["missing"]})


def test_schemas_are_valid_and_match_runtime(tmp_path, spec, project):
    export_schemas("all", tmp_path)
    export_schemas("all", tmp_path, check=True)
    for name in SCHEMAS:
        schema = json.loads((tmp_path / f"{name}.schema.json").read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(spec.model_dump(mode="json"), json.loads((tmp_path / "workspec.schema.json").read_text()))
    jsonschema.validate(project.workflow.model_dump(mode="json"),
                        json.loads((tmp_path / "workflow.schema.json").read_text()))


def test_checked_in_schema_drift():
    export_schemas("all", Path(__file__).parents[1] / "schemas", check=True)


def test_cli_spec_and_validate(tmp_path, spec, project, capsys):
    path = tmp_path / "spec.json"
    path.write_text(spec.model_dump_json())
    assert main(["spec", "validate", str(path)]) == 0
    assert main(["--project", str(project.root), "validate"]) == 0
    assert "Valid" in capsys.readouterr().out

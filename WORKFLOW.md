---
schema_version: 1
tracker:
  repository: intMinsu/symphony-research-agent-orch
  token_env: GITHUB_TOKEN
  ready_label: agent-ready
base_ref: main
planner:
  provider: claude
executor:
  provider: codex
runners:
  codex:
    command:
    - codex
  claude:
    command:
    - claude
checks:
  unit:
    argv:
    - python
    - -m
    - pytest
    - -q
    timeout_seconds: 900
  schemas:
    argv:
    - python
    - -m
    - sra
    - schema
    - all
    - --output-dir
    - schemas
    - --check
required_checks:
- unit
- schemas
limits:
  max_concurrent_runs: 1
  max_attempts: 3
---

# Research execution workflow

Read the approved spec before implementation. Inspect partial work before repeating experiments.
Read AGENTS.md or CLAUDE.md and .agent/POLICY.md. Stay inside the approved scope.
Do not edit acceptance criteria, policy files, baselines, or unrelated datasets.
Record commands, seeds, dataset versions, metrics, limitations, and artifact locations.
Negative or inconclusive results are valid research outcomes; never fabricate success.
Do not start detached training jobs. Report resource requirements as blockers.
The host runs configured checks and owns commits, push, and draft PR creation.
Do not merge PRs or close issues. Report evidence for human acceptance review.

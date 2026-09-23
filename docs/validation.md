# Validation record

## Executable tests

The automated suite uses real temporary Git repositories and subprocesses, fixture Codex/Claude executables, an in-memory fake tracker for orchestration tests, and isolated SQLite databases. Tests do not consume model quota or mutate a real research repository.

Coverage includes:

- WorkSpec/Workflow validation, unknown fields, invalid paths, duplicate criteria and YAML keys.
- Canonical Issue round trips and generated JSON Schema validity/drift.
- Unknown CLI versions with supported surfaces; missing required security surfaces.
- Optional event tolerance, terminal event requirements, provider-reported failure.
- Environment filtering, secret redaction, bounded output, stdin streaming, timeout and cancellation.
- Atomic duplicate claims, legal transitions, explicit retry, approval revisions and conservative recovery.
- Issue-to-clone-to-agent-to-host-check execution for both providers.
- Original checkout preservation and explicit provider switching with a new native session.
- Scope and policy protection, changed remote contracts, closed Issues and dirty source checkouts.
- Delivery compare-and-set, post-verification changes, local commit and retry after a simulated push failure.
- Planning producing an unpublished, schema-valid draft.

Run:

```bash
python -m pytest -q
python -m sra schema all --output-dir schemas --check
python -m compileall -q src
```

## Observed local result

On Linux with Python 3.13.5, Pydantic 2.13.4, PyYAML 6.0.3, and pytest 9.0.2:

- All 59 tests passed in bounded groups: 43 contract/runner/store tests and 16 engine integration tests (5 + 11).
- JSON Schema drift checks and Python compilation checks passed.
- A wheel was built without dependency downloads, installed into a separate target, and its CLI reported version 0.1.0.

The execution environment imposed per-command limits, so the complete suite was validated in groups rather than claiming a single uninterrupted full-suite run. Python 3.11/3.12, macOS, and GitHub-hosted CI were not executed in that local environment. CI configuration includes Python 3.11, 3.12, and 3.13; its status must be checked separately. Ruff was not available locally and no lint pass is claimed.

## Not validated by fixture tests

A fixture CLI is not Codex or Claude Code. Real CLI authentication, actual model/effort availability, provider-specific permission enforcement, billing, local plugins, native session internals, and live GitHub push/PR behavior require operator integration tests.

The initial development environment did not have authenticated Codex or Claude binaries. It also did not have the consuming research repository, datasets, GPU environment, or runtime GitHub credential available. No real research task or model run was performed as part of this validation.

The repository publication of this implementation is separate from a live SRA end-to-end test. Creating these source files through a GitHub connection does not validate SRA's REST and Git-push adapter against the user's runtime credentials.

## Suggested real-host acceptance exercise

Use a disposable repository with no private data. Run `init`, configure a small deterministic check, commit policy to the base, and perform an explicit `doctor --smoke` for each installed provider. Publish a tiny approved spec. Run without delivery, inspect the diff and logs, test cancellation on a disposable task, then explicitly deliver a draft PR. Rework the same Issue with the other provider and reviewed feedback.

Record the exact observed CLI versions, resolved environment, model access, and permission behavior. Only after this should a real research repository be connected. This is a local acceptance exercise, not an unattended upgrade job.

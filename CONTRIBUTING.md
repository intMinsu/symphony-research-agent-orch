# Contributing

Install Python 3.11+ and use an isolated virtual environment. Install with `python -m pip install -e '.[dev]'`.

Run tests with `python -m pytest -q`; tests must not require provider credentials or a live GitHub token. Add fixture-based tests first and label any real-provider integration test explicitly as opt-in.

Public contract changes belong in `models.py`. Regenerate schemas with `sra schema all --output-dir schemas` and verify drift with `--check`. Changes to SQLite ownership semantics need concurrency and interrupted-run tests, not only happy-path tests.

Keep provider-specific argv/event handling inside the adapter. Core orchestration must not branch on raw native event names. Do not solve compatibility by hardcoding a vendor version allowlist, dropping security requirements, or silently substituting a model.

Keep README.md in Korean and all other documentation in English. Document operational limitations before describing a feature as supported. Do not add private research content, credentials, generated run logs, or datasets to this repository.

This alpha deliberately omits distributed scheduling, native cross-attempt resume, automatic rebase/cleanup, automatic PR-review loops, and automatic merging. Proposals in these areas must include failure semantics and maintenance costs, not only an interface sketch.

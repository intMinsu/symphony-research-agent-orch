# Changelog

## 0.1.0 — Initial alpha

- Independent Python CLI with project-owned WORKFLOW.md and instruction templates.
- Generated schemas for workflow, work specification, capabilities, normalized events, and execution records.
- Codex exec JSONL and Claude print stream-JSON adapters without a CLI version allowlist.
- Explicit model/effort selection and conservative required-surface checks.
- Planning drafts, canonical spec Issues, local hash-bound approval and publication locking.
- Independent Git workspaces, SQLite atomic claims, cancellation, bounded streams and explicit retry.
- Trusted host checks, delivery fingerprints, immutable-SHA push and draft PR handoff.
- Local watcher, retained research artifacts, Korean README and English design/operations/security documentation.

Known limits: trusted local execution only; no native session resume, distributed controller,
automatic experiment retry, automatic review loop, automatic merge, or GPU/container provisioning.

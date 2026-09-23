# Instructions for contributors and coding agents

This repository implements a standalone research orchestrator, not a research project.

- Keep research-specific knowledge in consuming repositories, not the coordinator.
- Keep provider protocols inside `runners.py`; core logic consumes normalized events only.
- Do not add a CLI version allowlist or silently downgrade security/model/effort settings.
- Preserve explicit local approval, atomic run ownership, and host-owned verification/delivery.
- Never enable automatic merge, skip-permissions flags, or automatic destructive workspace cleanup.
- Treat shell-capable project code as trusted execution, not as sandboxed merely because it has a cwd.
- Add tests for every new CLI event family, state transition, schema change, and recovery path.
- Generate JSON Schemas from runtime models; do not hand-edit schema JSON.
- Keep README.md in Korean. Keep other documentation and code comments in English.
- Run `python -m pytest -q`, `python -m sra schema all --output-dir schemas --check`, and compilation checks.
- Do not claim real-provider compatibility based only on fake CLI tests.

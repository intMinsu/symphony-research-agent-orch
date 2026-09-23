# Operations

## Before first use

Use Linux/macOS, Python 3.11+, Git, and an independently authenticated Codex and/or Claude Code CLI. Install SRA outside the research checkout. The selected `--project` must be the actual Git root.

Run `init`, review all generated policies, configure check commands and the base branch, and commit policy files into that base. Existing instruction files are preserved, not automatically rewritten. The executor clones committed content; untracked datasets, virtual environments, local caches, and working-tree edits are not copied.

Prepare dependencies using your existing research environment. A check such as `uv run --frozen pytest` can be configured by the repository owner, but it is an explicitly trusted host command. SRA has no separate automatic setup-hook engine in 0.1.0.

## Command reference

Global options precede the command: `sra --project /repo --state-dir /private/state <command>`. The default state root is `$XDG_STATE_HOME/sra`, or `~/.local/state/sra`. State inside the research repository is refused.

| Command | Effect |
|---|---|
| `init --repository OWNER/REPO` | Writes missing project templates; never overwrites an existing workflow |
| `validate` | Checks trusted workflow configuration without running commands |
| `spec validate FILE` / `spec render FILE` | Validates a JSON draft or renders Markdown |
| `schema all --output-dir schemas [--check]` | Generates schemas or detects drift |
| `doctor --provider ... --trust-project [--smoke]` | Inspects configured CLI surfaces; smoke calls a model |
| `plan --id ... --request ... --output FILE --trust-project` | Creates an unpublished draft through the selected CLI |
| `publish FILE [--ready]` | Creates/updates an Issue; ready also grants local approval |
| `inspect NUMBER` | Displays canonical remote spec and digest |
| `approve NUMBER --spec-sha SHA` | Approves the inspected spec against the current local policy |
| `run NUMBER --trust-project` | Executes a single approved Issue without automatically pushing |
| `run NUMBER --retry --provider ... --trust-project` | Explicit new attempt in the same workspace |
| `run ... --feedback-file FILE` | Supplies reviewed rework input; automatic PR feedback scraping is not implemented |
| `deliver NUMBER --trust-project` | Commits/pushes verified content and opens/reuses a draft PR |
| `watch --trust-project [--once]` | Polls locally approved Issues with bounded concurrency |
| `status [--json]` / `logs RUN_ID` | Reads local state and normalized events |
| `cancel RUN_ID` | Writes a cancellation request for the active supervisor |
| `recover RUN_ID --confirm-stopped` | Releases an orphaned local run after explicit process checks |

`--publish-pr` on run/watch is an explicit request for delivery after verification. No command auto-merges a PR or automatically closes an Issue.

## Credentials

The GitHub adapter uses `GITHUB_TOKEN` by default, with `GH_TOKEN` as a fallback. A custom `tracker.token_env` is supported. Scope the credential to the research repository and required Contents, Issues and Pull requests permissions.

The GitHub token is used by the host REST adapter and a transient host Git push environment. It is not inserted into argv, persisted as a Git remote credential, or intentionally passed to model children. Provider API keys are passed only to their selected CLI. Additional environment variables require an explicit provider passthrough list.

The host HOME and native credential files may still be visible to trusted local processes. Environment filtering is not credential isolation. See [SECURITY.md](../SECURITY.md).

## Retry and reconciliation

There are two different retry classes:

- Repository GET requests have a small bounded backoff for transient transport/server errors. Writes are not blindly replayed.
- Agent tasks are not automatically retried. A repeated `run` requires `--retry` and respects the per-spec attempt budget.

The watcher skips attempted specs, including failed and awaiting-review runs. It will dispatch a newly approved revision after the previous attempt is terminal. While supervising active work, it cancels a run when the Issue disappears from eligible candidates. Tracker outages pause new dispatch rather than treating the outage as evidence that all tasks were closed.

A one-shot run rechecks the Issue before starting and before handoff; it does not continuously poll during the model call. Use watch for eligibility reconciliation during execution.

## Cancellation and shutdown

`cancel` creates a local marker. Agent and validation supervisors check it regularly, terminate the POSIX process group, and escalate to SIGKILL when necessary. Setup Git operations observe cancellation at the next engine boundary and have their own timeout.

Ctrl-C and CLI SIGTERM handling stop supervised children. A hard crash, SIGKILL, host failure, or a child that creates a separate session may leave processes behind. No timeout automatically releases a run claim.

Before recovery, inspect the run's host, owner PID, child PID/process group, workspace, and any resource-intensive descendants. `recover --confirm-stopped` rejects still-existing PIDs/groups and foreign-host records. PID reuse can produce a conservative refusal. Never kill an unrelated reused PID merely to satisfy recovery; inspect the state with a backup instead.

## Delivery failure

Delivery claims the run by compare-and-set, verifies the content fingerprint, commits locally with host Git hooks disabled, checks content again, records the commit, then pushes the immutable SHA without force.

If push or PR creation fails, the run returns to `awaiting_review` with an error. Correct authentication/network issues and repeat `deliver`; the model does not need to run again. Existing open matching PRs are reused. A closed/merged or base-mismatched PR on the managed branch is not silently repurposed.

Do not edit a workspace between verification and delivery. A changed fingerprint requires another explicit run/revalidation. Human edits to an existing PR description are preserved.

## Research artifacts

A `report` task retains the result and workspace locally without creating a PR. Keep large datasets/checkpoints out of Git and declare repository-relative artifact references in the spec. SRA does not mount datasets, schedule GPUs, monitor detached training jobs, or independently judge metric validity.

Workspace cleanup is manual. The tool intentionally does not delete artifacts just because a ticket was closed. Inspect and archive results before removing workspaces or state.

## Troubleshooting

| Symptom | Explanation / safe action |
|---|---|
| Unknown CLI version | Not itself an error. Check the missing-surface list and run an explicit smoke test. |
| Missing required flag | Install a compatible CLI or change the explicitly requested optional feature. Never bypass required security controls. |
| Exit 0 without terminal event | Adapter/runtime mismatch or truncated stream. Inspect events; do not mark the task complete. |
| Spec/policy not approved | Inspect the current digest and approve the exact contract. Increment revision for changed spec content. |
| Source checkout is dirty | Commit or stash local changes. SRA will not copy arbitrary uncommitted input. |
| Policy differs from base/workspace | Ensure new tasks use committed policy at the selected base. Existing workspaces are not automatically migrated/rebased; finish them or create a new task on the new base. |
| Active run after a crash | Inspect processes, stop orphaned work safely, then explicitly recover. |
| Scope/protected-file failure | Inspect the diff. Do not broaden permissions simply to hide unexpected changes. |
| No automated checks | The template is intentionally empty. Register trustworthy checks before claiming automated verification. |
| Costly experiment failed | Decide whether retry is scientifically appropriate; automatic task retry is off. |

## Reproducibility and backups

Archive the spec/policy snapshots, execution selection, observed CLI version, base SHA, delivered commit SHA, check logs, and research artifact references. Model aliases and provider backends can change; recording an alias alone does not guarantee reproducible model behavior.

Back up SQLite with its backup API or while all SRA processes are stopped. Do not copy just the live database while discarding its WAL. Treat logs and backups as private research material. Do not share a live state directory over NFS or use it as a multi-host scheduler.

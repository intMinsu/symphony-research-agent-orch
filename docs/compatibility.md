# CLI compatibility

## The guarantee is deliberately narrow

SRA does not maintain a Codex/Claude version allowlist. It probes the configured executable with `--version` for diagnostics and `--help` for surface evidence. Codex also receives `exec --help`.

Help evidence does not prove authentication, model access, flag value support, sandbox effectiveness, plugin behavior, native session compatibility, or future protocol stability. `doctor --smoke` makes a real read-only-intent call in a temporary Git repository and requires a successful terminal event plus the answer `OK`. It consumes model quota. It is still not a complete coding/permission conformance test.

The initial automated tests use fixture CLIs, including an unrecognized future version string. No real CLI version is certified by this release's local tests.

## Transport choices

| Provider | Transport | Required observed surfaces |
|---|---|---|
| Codex | `codex exec --json`, prompt on stdin | `--json`, `--sandbox`, `--ask-for-approval`, `--config` |
| Claude Code | print mode, `stream-json`, prompt on stdin | `--print`/`-p`, `--output-format`, `--verbose`, `--permission-mode`, `--tools`, `--allowedTools`/`--allowed-tools`, `--strict-mcp-config` |

`--model` is required when a model is configured. Claude `--effort` is required when effort is configured. Codex effort is passed as a quoted config value. The coordinator does not invent model IDs or translate effort scales across providers.

A required missing surface is a compatibility error, even on a very new version. An unrecognized version string with the needed surfaces is allowed to attempt execution. If native flag values or a configured model are rejected, the run fails visibly; no silent model/effort/security fallback occurs.

## Security-sensitive behavior

Codex receives `--ask-for-approval never` with an explicit read-only or workspace-write sandbox. `never` means unattended execution must not pause for user approval; it does not disable the sandbox. Workspace network access is explicitly configured where applicable.

Claude receives `dontAsk`, a built-in tool list, explicit allowed tools, and strict MCP configuration with no project MCP servers loaded by SRA. The default execution list includes Bash. These are tool permission choices, not the same filesystem/network isolation as Codex's sandbox.

Do not replace a missing security feature with `danger-full-access`, `bypassPermissions`, or a broad skip-permissions flag. Run only trusted code, and introduce external isolation when required.

## Events

Codex success requires `turn.completed` and exit status 0 with no `turn.failed`. Claude success requires a `result` with subtype `success`, no error, and exit status 0. The completion predicate never searches terminal prose for "done".

New optional events become diagnostics. Native event shapes can still break adapters; this is compatibility tolerance, not unlimited forward/backward compatibility. Keep fixture streams for every newly validated real CLI family and run local smoke/integration checks when upgrading.

## Provider switching

Every attempt starts a new native conversation. The input includes the approved spec, project workflow, previous result summary when available, and any explicitly supplied feedback file. The workspace and branch persist. This avoids treating Codex and Claude session storage as interoperable.

Native session resume, App Server interactive approvals, SDK backends, arbitrary raw flags, and dynamic MCP tools are not implemented in this release. Keep such changes inside the adapter boundary rather than leaking provider events into the scheduler.

## Upgrade procedure

1. Keep the previous installed CLI executable available.
2. Run `doctor --provider ... --trust-project` against the new command.
3. Run the explicit, quota-consuming `--smoke` check.
4. Execute a small approved task in a disposable research repository with a real check and no private data.
5. Confirm cancellation, scope checks, provider authentication, and draft delivery using a disposable branch.
6. Record the tested CLI version and observed limitations. Do not call a help-only probe a certified version.

## Primary references

- [OpenAI Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)
- [OpenAI Codex CLI reference](https://developers.openai.com/codex/cli/reference)
- [Claude Code programmatic execution](https://code.claude.com/docs/en/headless)
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)
- [OpenAI Symphony specification](https://github.com/openai/symphony/blob/main/SPEC.md)

These references informed the adapter design. Their current contents are not a substitute for checking the binaries installed on the execution host.

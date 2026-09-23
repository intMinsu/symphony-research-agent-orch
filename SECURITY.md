# Security and trust model

## Intended environment

SRA 0.1.0 is for a single user's **trusted local research repositories**. It is not a hosted service for arbitrary public Issues, untrusted pull requests, or adversarial code. Do not run it against unreviewed repositories with valuable host credentials or sensitive datasets available.

`--trust-project` acknowledges this boundary. It does not create a sandbox, authorize arbitrary disclosure, or guarantee that a model follows Markdown instructions.

## Controls implemented

- Remote labels alone cannot authorize execution. Exact spec and local policy digests require local approval.
- WorkSpec JSON cannot define host shell commands; it references owner-configured check IDs.
- CLI arguments are arrays; the coordinator does not interpolate prompts into a shell.
- Codex uses an explicit sandbox and unattended approval policy; missing required surfaces are not silently ignored.
- Claude uses explicit tool restrictions, `dontAsk`, and strict MCP configuration. No skip-permissions mode is enabled by SRA.
- Independent Git clones avoid shared mutable Git metadata and preserve the original checkout.
- Changed paths, symlinks, file sizes, protected policy paths, and the generated spec are checked before delivery.
- Content is fingerprinted before/after commit and an immutable commit SHA is pushed without force.
- Runtime state is private and outside the research checkout. Known secret environment values are redacted from logged text.
- Tracker credentials are excluded from model-child environment passthrough. Git push credentials are transient host environment values, not command arguments or persisted remote credentials.
- Child streams and command runtimes are bounded. Ordinary descendants are stopped through their POSIX process group.
- No automatic merge, automatic Issue close, credential export tool, or destructive workspace cleanup is implemented.

## Important non-guarantees

**A workspace is not an OS security boundary.** Shell tools and host validation commands can execute arbitrary trusted project code. Claude's allowed Bash tool is not equivalent to Codex's filesystem sandbox. Permission configuration must not be silently weakened to accommodate old clients.

HOME, provider credential files, Git configuration, external data, local services, repository hooks/plugins, and environment-dependent tooling may still be visible or executable. Stripping an environment variable does not make disk credentials inaccessible. Project instructions, settings, skills, MCP configuration, and tests can carry malicious instructions or executable behavior.

Host Git hooks are disabled for SRA's own Git commands, but this does not neutralize every Git filter or all native CLI hooks. Path checks are post-execution publication guards; they do not prevent exfiltration or unauthorized filesystem access during execution. Provider configuration may grant access beyond the current working directory.

Help parsing is not attestation. Smoke tests do not prove sandbox enforcement. Model instructions are not permission controls. GitHub comments, Issue prose, model output, and feedback files remain untrusted task data.

A public repository's workpad and PR are public surfaces. SRA publishes only bounded status/check summaries by default, not raw transcripts or local paths. Review actual code and artifact content before delivery; this is not a full secret scanner.

## Recommended operator posture

Use narrowly scoped GitHub credentials, separate research credentials from unrelated personal credentials, and avoid mounting sensitive data unnecessarily. For riskier workloads, supply an isolated host/container/VM and restrict network and filesystem access outside SRA. Host validation must run within the same intended security boundary if isolation is required.

Run model/CLI upgrades against a disposable repository first. Keep task scope and budgets small. Review every scientific claim and every proposed merge. Never treat a successful test suite as evidence that a negative/positive research hypothesis is true.

If a run requests broader permissions, unknown external execution, or sensitive access, stop and inspect it. Do not fix compatibility by adding a bypass flag.

## Reporting

Do not include secrets, private datasets, raw model transcripts, or sensitive workspace paths in a public Issue. Report only a minimal sanitized reproduction through an appropriate private channel with the repository owner before disclosing sensitive details publicly.

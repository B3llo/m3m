# Privacy and threat model

## Protected assets

The primary sensitive assets are transcripts, durable memories, handoffs, diagnostics, provider configuration, and optional API credentials. Project `.agent-m3m/` stores and generated root `AGENTS.md` or `MEMORY.md` exports are ignored by default and must not be committed.

## Trust boundaries

- The local operating-system account and filesystem are trusted.
- Configured agent CLIs and direct API providers are trusted only when explicitly enabled.
- Transcript content is untrusted input. It must never become a shell command.
- Hook configuration is user-owned. Install and uninstall operations modify only recognized m3m entries and fail closed on malformed files.
- MCP clients are external callers. Tool annotations must match actual mutation behavior.

## Controls

- Config files are atomically written with mode `0600` on POSIX systems.
- Common credential formats are scrubbed before memory persistence.
- External commands use argument arrays without `shell=True`.
- Hook capture is bounded and queued to avoid provider processes surviving agent timeouts.
- Bulk archival requires an interactive terminal and remains reversible.
- Runtime data is excluded from source control, but already tracked files require an explicit Git removal.

Secret scrubbing is defense in depth, not a guarantee. Review memories before sharing, prefer environment variables over literal API keys, and report disclosure or corruption risks through the private process in [SECURITY.md](../SECURITY.md).

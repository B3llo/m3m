# Changelog

All notable changes to agent-m3m will be documented in this file. The project follows [Semantic Versioning](https://semver.org/) and the structure of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Corroboration: re-observed memories strengthen their keeper instead of piling files, with a bounded ranking bonus.
- Multilingual query expansion (prefix morphology plus a curated EN/PT/ES/FR/DE/IT synonym map with per-project `synonyms.md` overlays), applied only when strict FTS underfills; disable with `query_expansion = false`.
- Contradiction guard: near-duplicates with asymmetric negation, divergent numbers, or swapped antonyms route to the LLM instead of a silent skip.
- Hook-log rotation, bounded diagnostics reads, and `m3m clean --prune-logs/--prune-queue` (dry-run by default).
- `m3m context --no-record-usage` for high-frequency hooks.
- Doctor hygiene checks (config permissions, log and queue sizes).
- Reconcile (73 cases, adversarial families) and semantic (zero-overlap) evaluation corpora with gates.
- CI coverage enforcement, distribution validation, and clean wheel smoke testing.
- Contribution, security, community, and release documentation.
- Dependabot configuration for GitHub Actions and Python development dependencies.
- Selective hook installation and uninstall commands that preserve unrelated settings.
- Versioned retrieval quality evaluation with Recall@5 and MRR gates.
- Architecture, privacy, operations, and memory-format references.

### Changed

- Package metadata now declares the supported macOS and Linux platforms without claiming Windows support.
- Local `.agent-m3m` runtime data and generated `MEMORY.md` are excluded from source control.
- GitHub Actions are pinned to immutable revisions and have bounded execution time.
- Installation uses isolated `uv` or `pipx` environments and never initializes state implicitly.

### Fixed

- Package and module versions now consistently report `0.3.0`.
- Corrected a typo in the MIT License disclaimer.
- Config files are atomically written with private permissions and TOML strings round-trip safely.
- Hook installers fail closed on malformed configuration and retain the Codex session-end safety contract.
- MCP read-only context no longer records usage or rewrites memory files.

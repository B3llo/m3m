# agent-m3m (`m3m`)

> **Local-first persistent memory for AI coding agents with Markdown as the Source of Truth.**

`m3m` is an open-source, preview-environment memory framework designed for developers using AI coding agents (**OpenAI Codex CLI**, **Claude Code**, **OpenCode**, **Kimi CLI**, **Kiro**, **Gemini CLI**, and others).

It gives you a **universal personal memory** across different agents without locking you into a single tool or requiring external SaaS, vector databases, or background servers.

---

## 💡 Core Philosophy

```text
               Coding Agent
                    │
         ┌──────────┴──────────┐
         │                     │
     retrieval             extraction
         │                     │
         ▼                     ▼
   MemoryRetriever       MemoryExtractor
         │                     │
         └──────────┬──────────┘
                    ▼
               MemoryStore
                    │
                    ▼
                MARKDOWN
            SOURCE OF TRUTH
                    │
                    ▼
          disposable index (SQLite FTS5)
```

1. **Markdown = Source of Truth (SSOT):** All durable memories are saved as human-readable Markdown files with YAML frontmatter in `~/.agent-m3m/` (global) or `<repo>/.agent-m3m/` (project).
2. **Disposable Indexes:** All search indexes (SQLite FTS5) are derived. If deleted, `m3m reindex` rebuilds them 100% faithfully from your Markdown files.
3. **Progressive Disclosure:** Injects concise **Core Memory** at session start, and retrieves topic-specific **Archival Memories** dynamically per prompt or query.
4. **Deterministic Secret Sanitization:** Regex-based scrubbing redacts common API keys, tokens, passwords, connection strings, and private keys. It is a safety layer, not a formal guarantee, so review proprietary content before enabling an external provider.
5. **Conflict & Evolution Management:** Superseded preferences (e.g. switching from `npm` to `orbitpm`) mark old memories as `superseded` rather than corrupting history or creating duplicate noise. Extraction reconciles candidates against similar active memories (`ADD`/`UPDATE`/`SUPERSEDE`/`SKIP`), and near-duplicates (Jaccard ≥ 0.8) are dropped deterministically without an LLM call.
6. **Confidence-based Auto-Approval:** Session-end extractions with `confidence >= auto_approve_confidence` (default `0.9`, in `config.toml` `[general]`) become active immediately; lower-confidence captures and contradictions stay `pending` for review (`m3m approve`, `m3m approve --all [--min-confidence N]`, reject with `m3m forget --pending`).
7. **Value Signals & Consolidation:** Memories injected into context earn `use_count`/`last_used` feedback (bounded to once per day per item), which feeds ranking and `m3m consolidate`: it rebuilds `CORE.md` from the most valuable memories (pinned > used > trusted > recent) within a line budget, and reports never-used candidates for archival.

---

## 🚀 Quick Start

### 1. Installation

Clone the repository and run the installer:

```bash
git clone https://github.com/your-username/agent-m3m.git
cd agent-m3m
./install.sh
```

Or install using `pip`:

```bash
pip install -e .
```

### 2. Initialize Memory

Initialize your global memory store (`~/.agent-m3m/`):

```bash
m3m init --global
```

Initialize project-level memory store (`.agent-m3m/`) inside any repository:

```bash
cd ~/Documents/Projects/my-project
m3m init
```

`init` creates empty storage only. It does not import legacy memories or edit agent configuration unless agents are selected explicitly:

```bash
m3m init --agents codex,claude
m3m import --path ./CLAUDE.md
```

### Migration from other tools

`m3m import` understands real-world sources and keeps everything as Markdown:

- **Claude Code native auto memory** (`~/.claude/projects/*/memory/` — `user`/`feedback`/`project`/`reference` types are mapped to m3m types)
- **CLAUDE.md / AGENTS.md** (any flavor): sections, bullets and prose — no more 1000-char drop
- **Cursor** (`.cursorrules`, `.cursor/rules/*.mdc`), **Windsurf** (`.windsurfrules`, `.windsurf/rules/`), **Cline** (`.clinerules`)
- **claude-mem / jayzeng/agentmemory** directories and **claude-mem SQLite databases** (`observations`/`summaries` tables, read-only, schema-agnostic) and **mem0-style JSON exports** (`{"memories": [...]}` with `content`/`memory`/`text` keys)

Imports are fuzzy-deduplicated: paraphrases of memories you already have (or of other rows in the same batch, Jaccard ≥ 0.80) are dropped instead of piling up. Preview before writing with `m3m import --dry-run`; imports are idempotent. Clean up historical duplicates with `m3m dedup [--apply]`. Export back out with `m3m export --format agents-md`.

### Universal exports (`m3m export-universal`)

Turn your memory store into the Markdown files agents already read natively — for teammates, other machines, or tools without m3m hooks:

| Target | File | Notes |
| :--- | :--- | :--- |
| `agents` (default) | `AGENTS.md` | Read by Codex, Cursor, Copilot, Gemini CLI, Windsurf/Devin, Zed, opencode, goose, Aider and more |
| `memory` (default) | `MEMORY.md` | One-line-per-memory index (≤200 lines/25KB), Claude Code auto-memory style |
| `claude` | `CLAUDE.md` | Same managed content |
| `gemini` | `GEMINI.md` | Same managed content |
| `cursor` | `.cursor/rules/m3m-memory.mdc` | `alwaysApply: true` frontmatter |
| `windsurf` | `.windsurf/rules/m3m-memory.md` | `trigger: always_on` (≤12k chars) |
| `copilot` | `.github/copilot-instructions.md` | Copilot code review / chat |
| `cline` | `.clinerules/m3m-memory.md` | Cline / RooCode |
| `kiro` | `.kiro/steering/m3m-memory.md` | `inclusion: always` |

Guarantees:

- **Managed block**: generated content lives between `<!-- m3m:start -->` and `<!-- m3m:end -->`. Existing files without the marker get the block *appended* — your hand-written content is never touched. Re-running replaces only the block.
- **Bounded selection**: pinned → most used → most confident → newest, capped by `export_universal_max_lines` (default 150) — never a 500-memory dump.
- **No feedback loops**: `m3m import` skips managed blocks, so re-importing an export adds nothing; manual edits *outside* the block still import normally.
- `.agent-m3m/` remains the single source of truth; exports are disposable derived files.

### MCP server (any MCP client)

`m3m mcp` runs a zero-dependency MCP stdio server over the same Markdown store:

```bash
# Claude Code
claude mcp add m3m -- m3m mcp

# Codex CLI (~/.codex/config.toml)
# [mcp_servers.m3m]
# command = "m3m"
# args = ["mcp"]

# Cursor (.cursor/mcp.json)
# {"mcpServers": {"m3m": {"command": "m3m", "args": ["mcp"]}}}
```

Tools: `m3m_context`, `m3m_search`, `m3m_show`, `m3m_remember`, `m3m_pin`, `m3m_forget` — annotated as read-only/writing/destructive so agents pick the right one.

### 3. Configure the LLM Provider

Memory extraction and semantic maintenance run through the configured provider. `api` calls an OpenAI-compatible chat completions endpoint directly over HTTP — no CLI, no agent sessions, nothing shows up in your coding agent's chat history:

```toml
# ~/.agent-m3m/config.toml
[providers]
active_provider = "api"
model_name = "glm-5.3-flash"
api_base_url = "https://api.z.ai/api/coding/paas/v4/chat/completions"
api_key_env = "ZAI_API_KEY"   # or set api_key directly (local file, not committed)
```

CLI-based providers (`codex`, `claude`, `opencode`, `kimi`) remain available, but they spawn visible sessions in the corresponding tool. `m3m init --provider api --model glm-5.3-flash` applies the API provider non-interactively.

### 4. Check System Health

```bash
m3m doctor
```

---

## 📖 CLI Command Reference

| Command | Description |
| :--- | :--- |
| `m3m init [--global]` | Initialize global (`~/.agent-m3m`) or local project memory (`.agent-m3m`). Use `--agents` to install hooks explicitly. |
| `m3m import [--path PATH]` | Explicitly import memories from a legacy tool or steering file. |
| `m3m context [--prompt "..."]` | Output bounded Core Memory plus prompt-specific archival retrieval. |
| `m3m search "<query>"` | Fast local FTS5 search with BM25 + metadata ranking. Supports `--scope`, `--status`, `--json`, and `--debug`. |
| `m3m save [--stdin]` | Extract and save durable memories. The Codex hook uses direct `Stop` capture; `--require-approval` keeps extracted items pending. |
| `m3m list` | List active memories grouped by type with topic notes. Cut previews end in `…`; use `--full` for everything, `--topic SLUG` / `--type TYPE` to filter. |
| `m3m show <id>` | Print full Markdown & YAML frontmatter of a memory by ID. |
| `m3m forget <id>` | Archive a memory by setting its status to `archived`. |
| `m3m forget --pending` | Reject all pending memories at once (interactive confirmation; reversible via `m3m restore <id>`). |
| `m3m approve [id]` / `m3m approve --all [--min-confidence N]` | Approve pending memories (single, batch, or by confidence floor). |
| `m3m pin <id>` / `m3m unpin <id>` | Pin a memory so it is always injected into context; `unpin` removes it. |
| `m3m consolidate [--apply]` | Rebuild `CORE.md` from the most valuable active memories (dry run by default) and report never-used candidates. |
| `m3m link <id> <other-id>` / `m3m related <id>` | Connect two memories bidirectionally and walk the relation graph one hop. |
| `m3m dedup [--apply] [--threshold N] [--semantic]` | Cluster near-duplicate active memories (Jaccard ≥ threshold, default 0.80), keep the most valuable variant, supersede the rest. With `--semantic`, the provider merges each cluster into one canonical English statement in place. Dry run by default; `m3m restore` reverses. |
| `m3m topics` | Show the topic taxonomy with active counts per scope. |
| `m3m topics --build [--apply]` | Compile a derived Markdown digest page per topic into `.agent-m3m/topics/` (LLM-compiled; deterministic fallback when the provider is unavailable). Dry run by default. |
| `m3m reorganize [--apply] [--scope ...]` | Assign topics to legacy untagged memories via the provider, preferring the configured seeds and creating new slugs when none fit. Dry run by default. |
| `m3m topic rename OLD NEW` / `m3m topic merge SOURCE TARGET` | Rename a topic everywhere or fold one topic into another (rewrites frontmatter and reindexes). |
| `m3m index [--apply]` | Regenerate the derived human-readable `INDEX.md` (the individual memory files remain the source of truth). |
| `m3m restore <id>` | Move an archived memory back to active. |
| `m3m reindex` | Rebuild SQLite FTS5 index completely from Markdown files. |
| `m3m clean` | Validate memory files and rebuild disposable index. |
| `m3m stats [--json]` | Show memory counts, parse errors, and the active project index path. |
| `m3m export --path PATH` | Export memories to JSON, Markdown bundle, or CLAUDE.md-style instructions (`--format agents-md`). |
| `m3m export-universal [--target ...] [--all] [--dry-run] [--limit N]` | Export to the universal agent files (AGENTS.md, MEMORY.md, per-tool rules) inside managed blocks that never clobber your content. |
| `m3m doctor` | Run diagnostic health checks on storage, config, and agent CLI binaries. |
| `m3m diagnostics [--limit 20]` | Show structured local outcomes and failures from memory-save hooks. |
| `m3m mcp` | Run the local MCP stdio server (any MCP client gets memory tools over the same Markdown store). |

---

## 🔍 Search & Debug Ranking Breakdown

Search uses a transparent scoring formula combining lexical BM25, entity matching, tag matching, project scope, status, and recency:

```bash
m3m search "orbitpm" --debug
```

---

## 🗂️ Topics & Memory Quality

Memories carry an optional `topic` slug (e.g. `storefront`, `billing-api`, `infra`) that groups related facts inside the same repository, so multiple subprojects stay separated.

- **Hybrid taxonomy:** seed preferred topics under `[topics]` in `~/.agent-m3m/config.toml` (`seeds = ["webstore", "billing-api"]`). The extractor prefers your seeds and may create a new slug when none fits; `m3m topic rename/merge` keeps the taxonomy tidy.
- **Layered injection:** `m3m context` always injects core rules, then groups prompt-relevant archival memories under their topic headers, so each block states which project/system it applies to.
- **Topic-aware ranking:** search boosts memories whose topic matches the prompt.
- **English canonical content:** extraction writes complete, self-contained English sentences that name the concrete project/system (never mid-thought fragments), with at most 8 memories per extraction.
- **Write-time dedup:** new candidates are reconciled against recent memories before saving (exact → Jaccard ≥ 0.80 skip → LLM reconcile in the 0.30–0.80 band).
- **Cleanup:** `m3m dedup` catches lexical duplicates; `m3m dedup --semantic` asks the provider to merge each cluster into one canonical statement. Both supersede (never delete) and stay reversible via `m3m restore`.
- **Legacy migration:** `m3m reorganize --apply` catalogues old untagged memories into the topic taxonomy; `m3m topics --build --apply` compiles each topic into a curated digest page.
- **Compiled digests in context:** once a topic page exists, `m3m context` replaces that topic's raw fragments with the bounded page digest (`max_topic_page_chars`, default 1200) — denser signal, fewer tokens.

---

## 🔌 Agent Integration (Hooks)

### Claude Code (`~/.claude/settings.json`)

```json
{
  "hooks": {
    "SessionStart": [
      {
        "type": "command",
        "command": "m3m context"
      }
    ],
    "SessionEnd": [
      {
        "type": "command",
        "command": "m3m save --stdin --session-end --quiet"
      }
    ]
  }
}
```

### OpenAI Codex CLI (`~/.codex/hooks.json`)

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear|compact",
        "hooks": [
          {
            "type": "command",
            "command": "m3m context --hook-output",
            "timeout": 3
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "m3m save --stdin --session-end --hook-output --quiet",
            "timeout": 3
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "m3m context --hook-output --hook-event UserPromptSubmit",
            "timeout": 3
          }
        ]
      }
    ]
  }
}
```

The Codex `Stop` hook only enqueues the completed response into the durable local queue, so it always returns the required protocol JSON within its 3s timeout. Extraction runs afterwards in a short-lived background worker that `m3m context` spawns automatically (no scheduler needed), or explicitly via `m3m queue process`. Details are written locally so hook stdout stays machine-readable:

```bash
m3m diagnostics
m3m diagnostics --json
```

The structured log is stored at `~/.agent-m3m/sessions/memory-save-diagnostics.jsonl`. It records sanitized outcomes such as `empty_hook_payload`, `malformed_hook_payload`, provider failures, and the number of memories saved. Queue and scheduler commands remain available for explicit recovery experiments, but they are not part of the official Codex capture path.

### OpenCode harness

Install the local OpenCode harness explicitly:

```bash
m3m init --agents opencode --non-interactive
```

It writes an ESM plugin to `~/.config/opencode/plugins/m3m-memory.js`, a directory OpenCode loads at startup. Memory is injected **before the first token of every turn**, matching the push-core/pull-tail pattern used by Zep, Letta and Claude Code: the `chat.message` hook stashes each new user prompt and rebuilds the context at most once per turn (with prompt-relevant archival retrieval), and the `experimental.chat.system.transform` hook injects the `m3m context` block into the system prompt on every LLM request — so the first response already sees memory, the block sits in the trusted system position, and it survives compaction. The `session.idle` hook captures user/assistant text after the session goes idle, excluding unchanged transcripts so it does not feed memory back into extraction.

Kimi remains available through `m3m init --agents kimi`. All integrations remain local and can be removed by restoring the `.bak` configuration files or deleting the generated plugin.

---

## 🛡️ Security & Git Policy

- **Global Memory (`~/.agent-m3m`):** Stored outside your git repositories.
- **Project Memory (`<repo>/.agent-m3m`):** High-level decisions can be committed if desired. Temporary session caches (`sessions/`) and disposable indexes (`.index/`) are automatically added to `.gitignore`.
- **Index isolation:** Each repository gets its own derived SQLite index. The index is automatically rebuilt when Markdown files change.
- **Secret Scrubbing:** Deterministic regex scrubber redacts common OpenAI/Anthropic, GitHub/GitLab, AWS, Slack, package registry, JWT, private-key, bearer, database, and environment-secret patterns before saving to Markdown.
- **Privacy:** `local_only = true` disables configured LLM providers and uses the deterministic extractor fallback. External CLIs are opt-in through configuration and explicit hook installation.

---

## 📜 License

Distributed under the [MIT License](LICENSE).

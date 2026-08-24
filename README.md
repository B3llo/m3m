# agent-m3m (`m3m`)

> **Local-first persistent memory for AI coding agents with Markdown as the Source of Truth.**

`m3m` is an open-source, local-first memory framework designed for developers using AI coding agents (**OpenAI Codex CLI**, **Claude Code**, **OpenCode**, **Kimi CLI**, **Kiro**, **Gemini CLI**, and others).

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
4. **Deterministic Secret Sanitization:** Regex-based scrubbing guarantees that API keys, tokens, and passwords never reach memory files.
5. **Conflict & Evolution Management:** Superseded preferences (e.g. switching from `npm` to `pnpm`) mark old memories as `superseded` rather than corrupting history or creating duplicate noise.

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

### 3. Check System Health

```bash
m3m doctor
```

---

## 📖 CLI Command Reference

| Command | Description |
| :--- | :--- |
| `m3m init [--global]` | Initialize global (`~/.agent-m3m`) or local project memory (`.agent-m3m`). |
| `m3m context` | Output combined Core Memory for session start injection. |
| `m3m search "<query>"` | Fast local FTS5 search with BM25 + metadata ranking. Use `--debug` for score breakdown. |
| `m3m save [--stdin]` | Extract and save durable memories from a session transcript. |
| `m3m list` | List all active stored memories by scope or status. |
| `m3m show <id>` | Print full Markdown & YAML frontmatter of a memory by ID. |
| `m3m forget <id>` | Archive a memory by setting its status to `archived`. |
| `m3m reindex` | Rebuild SQLite FTS5 index completely from Markdown files. |
| `m3m clean` | Validate memory files and rebuild disposable index. |
| `m3m doctor` | Run diagnostic health checks on storage, config, and agent CLI binaries. |

---

## 🔍 Search & Debug Ranking Breakdown

Search uses a transparent scoring formula combining lexical BM25, entity matching, tag matching, project scope, status, and recency:

```bash
m3m search "pnpm" --debug
```

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
    "Stop": [
      {
        "type": "command",
        "command": "m3m save --stdin"
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
        "type": "command",
        "command": "m3m context"
      }
    ],
    "Stop": [
      {
        "type": "command",
        "command": "m3m save --stdin"
      }
    ]
  }
}
```

---

## 🛡️ Security & Git Policy

- **Global Memory (`~/.agent-m3m`):** Stored outside your git repositories.
- **Project Memory (`<repo>/.agent-m3m`):** High-level decisions can be committed if desired. Temporary session caches (`sessions/`) and disposable indexes (`.index/`) are automatically added to `.gitignore`.
- **Secret Scrubbing:** Deterministic regex scrubber redacts OpenAI/Anthropic keys, GitHub tokens, AWS credentials, JWTs, private keys, and environment passwords before saving to Markdown.

---

## 📜 License

Distributed under the [MIT License](LICENSE).

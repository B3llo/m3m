# agent-memory

> **Local-first persistent memory for AI coding agents with Markdown as the Source of Truth.**

`agent-memory` is an open-source, local-first memory framework designed for developers using AI coding agents (**OpenAI Codex CLI**, **Claude Code**, **OpenCode**, **Kimi CLI**, **Kiro**, **Gemini CLI**, and others).

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

1. **Markdown = Source of Truth (SSOT):** All durable memories are saved as human-readable Markdown files with YAML frontmatter in `~/.agent-memory/` (global) or `<repo>/.agent-memory/` (project).
2. **Disposable Indexes:** All search indexes (SQLite FTS5) are derived. If deleted, `agent-memory reindex` rebuilds them 100% faithfully from your Markdown files.
3. **Progressive Disclosure:** Injects concise **Core Memory** at session start, and retrieves topic-specific **Archival Memories** dynamically per prompt or query.
4. **Deterministic Secret Sanitization:** Regex-based scrubbing guarantees that API keys, tokens, and passwords never reach memory files.
5. **Conflict & Evolution Management:** Superseded preferences (e.g. switching from `npm` to `pnpm`) mark old memories as `superseded` rather than corrupting history or creating duplicate noise.

---

## ⚡ Performance Benchmark

Benchmarked on local macOS machine with synthetic memory scale:

| Memories | FTS5 DB Size | Reindex Time | Avg Search Latency |
| :--- | :--- | :--- | :--- |
| **100** | 84 KB | 4 ms | **0.21 ms** |
| **1,000** | 504 KB | 27 ms | **0.70 ms** |

---

## 🚀 Quick Start

### 1. Installation

Clone the repository and run the installer:

```bash
git clone https://github.com/your-username/agent-memory.git
cd agent-memory
./install.sh
```

Or install using `pip`:

```bash
pip install -e .
```

### 2. Initialize Memory

Initialize your global memory store (`~/.agent-memory/`):

```bash
agent-memory init --global
```

Initialize project-level memory store (`.agent-memory/`) inside any repository:

```bash
cd ~/Documents/Projects/my-project
agent-memory init
```

### 3. Check System Health

```bash
agent-memory doctor
```

---

## 📖 CLI Command Reference

| Command | Description |
| :--- | :--- |
| `agent-memory init [--global]` | Initialize global (`~/.agent-memory`) or local project memory (`.agent-memory`). |
| `agent-memory context` | Output combined Core Memory for session start injection. |
| `agent-memory search "<query>"` | Fast local FTS5 search with BM25 + metadata ranking. Use `--debug` for score breakdown. |
| `agent-memory save [--stdin]` | Extract and save durable memories from a session transcript. |
| `agent-memory list` | List all active stored memories by scope or status. |
| `agent-memory show <id>` | Print full Markdown & YAML frontmatter of a memory by ID. |
| `agent-memory forget <id>` | Archive a memory by setting its status to `archived`. |
| `agent-memory reindex` | Rebuild SQLite FTS5 index completely from Markdown files. |
| `agent-memory clean` | Validate memory files and rebuild disposable index. |
| `agent-memory doctor` | Run diagnostic health checks on storage, config, and agent CLI binaries. |

---

## 🔍 Search & Debug Ranking Breakdown

Search uses a transparent scoring formula combining lexical BM25, entity matching, tag matching, project scope, status, and recency:

```bash
agent-memory search "pnpm" --debug
```

**Output:**
```text
Found 1 matching memories:

1. [0.90] mem_20260824_a1b2c3d4 (global/preference)
   Always prefer pnpm over npm or yarn in JS projects.
   Breakdown: {'lexical': 0.4, 'entity_bonus': 0.15, 'tag_bonus': 0.1, 'project_bonus': 0.0, 'status_bonus': 0.2, 'recency_bonus': 0.05}
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
        "command": "agent-memory context"
      }
    ],
    "Stop": [
      {
        "type": "command",
        "command": "agent-memory save --stdin"
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
        "command": "agent-memory context"
      }
    ],
    "Stop": [
      {
        "type": "command",
        "command": "agent-memory save --stdin"
      }
    ]
  }
}
```

---

## 🛡️ Security & Git Policy

- **Global Memory (`~/.agent-memory`):** Stored outside your git repositories.
- **Project Memory (`<repo>/.agent-memory`):** High-level decisions can be committed if desired. Temporary session caches (`sessions/`) and disposable indexes (`.index/`) are automatically added to `.gitignore`.
- **Secret Scrubbing:** Deterministic regex scrubber redacts OpenAI/Anthropic keys, GitHub tokens, AWS credentials, JWTs, private keys, and environment passwords before saving to Markdown.

---

## 📜 License

Distributed under the [MIT License](LICENSE).

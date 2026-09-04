# Memory file format

Each durable memory is a UTF-8 Markdown file with a small YAML-compatible frontmatter block and a plain-text body. JSON-compatible quoting is used for scalars and lists so files remain readable without a YAML dependency.

Required identity and classification fields are `id`, `scope`, `type`, `status`, and the Markdown body. Current writers also emit timestamps, confidence, topic, tags, entities, supersession, source, evidence, relations, expiration, usage feedback, and pin state.

Supported values:

- `scope`: `global` or `project`
- `type`: `preference`, `lesson`, `decision`, `convention`, `tool`, `project_fact`, or `workflow`
- `status`: `active`, `pending`, `superseded`, or `archived`

Readers tolerate missing optional fields and older hand-written scalar/list syntax. Unknown enum values fall back conservatively. IDs are stable references and filenames may move between status directories, so integrations should resolve memories by ID instead of constructing paths.

Example:

```markdown
---
id: "mem_20260904_1234abcd"
scope: "project"
type: "decision"
status: "active"
confidence: 0.95
tags: ["release"]
entities: ["agent-m3m"]
---

Release builds must pass the retrieval quality gate.
```

"""Importer / Migrator module for m3m.

Imports and migrates legacy memories from popular agent memory sources:
- Legacy Markdown and JSON memory directories
- Legacy SQLite memory stores
- Claude Code native auto memory (~/.claude/projects/*/memory/)
- Project steering files (CLAUDE.md, AGENTS.md, .cursorrules, .windsurfrules,
  .clinerules, .cursor/rules/, .windsurf/rules/, .clinerules/, .agent/rules/)
- Generic JSON exports ({"memories": [...]} or lists with
  content/memory/text keys)
"""

from pathlib import Path
from typing import List, Optional, Tuple
import json
import re
import sqlite3
import unicodedata

from .models import MemoryItem, MemoryScope, MemoryType, MemoryStatus, generate_memory_id
from .sanitizer import scrub_secrets
from .store import MemoryStore
from .retriever import MemoryRetriever
from .textutils import meaningful_tokens, jaccard
from .universal_export import MARKER_END, MARKER_START
from .config import Config


# Deterministic near-duplicate threshold shared with the extractor: imported
# candidates this similar to an existing (or batch) memory are dropped.
NEAR_DUPLICATE_JACCARD = 0.80
SQLITE_TEXT_COLUMNS = {"content", "observation", "summary", "memory", "text", "fact"}
SQLITE_PROJECT_COLUMNS = {"project", "project_id", "project_name", "repo"}
SQLITE_CREATED_COLUMNS = {"created_at", "timestamp", "created", "date"}


def _fold(text: str) -> str:
    """Lowercase and strip diacritics so 'Decisões' matches 'decisao'."""
    normalized = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


# Deterministic type classification from headings / filenames (pt + en).
TYPE_KEYWORDS = [
    (MemoryType.DECISION, (
        "decisão", "decisao", "decisoes", "decision", "decidido", "decided",
        "escolhido", "escolhida", "chosen", "adr", "architecture decision",
    )),
    (MemoryType.WORKFLOW, (
        "workflow", "comando", "command", "atalho", "shortcut", "how to run",
        "build", "deploy", "script", "fluxo",
    )),
    (MemoryType.LESSON, (
        "lição", "licao", "licoes", "lesson", "learned", "erro", "error", "bug",
        "fix", "pitfall", "gotcha", "cuidado", "warning", "armadilha",
    )),
    (MemoryType.CONVENTION, (
        "convenção", "convencao", "convencoes", "convention", "rule", "regra",
        "regras", "estilo", "style", "padrão", "padrao", "padroes",
    )),
    (MemoryType.TOOL, (
        "ferramenta", "tool", "stack", "biblioteca", "library",
    )),
    (MemoryType.PROJECT_FACT, (
        "fato", "fact", "contexto", "context", "visão", "visao", "overview",
        "referência", "referencia", "reference", "projeto", "project",
    )),
]
FOLDED_TYPE_KEYWORDS = [
    (mtype, tuple(_fold(keyword) for keyword in keywords))
    for mtype, keywords in TYPE_KEYWORDS
]

MAX_ITEMS_PER_FILE = 200
MAX_ITEM_CHARS = 1200
BULLET_PATTERN = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*)$")


def _strip_managed_blocks(body: str) -> str:
    """Remove m3m-managed export blocks so re-importing an export is a no-op.

    User-written content around the block still imports normally — that is
    the feedback loop; the generated block itself never feeds back.
    """
    lines: List[str] = []
    inside = False
    for line in body.splitlines():
        if MARKER_START in line:
            inside = True
            continue
        if MARKER_END in line:
            inside = False
            continue
        if not inside:
            lines.append(line)
    return "\n".join(lines)


class MemoryImporter:
    def __init__(self, store: Optional[MemoryStore] = None, config: Optional[Config] = None):
        self.config = config or Config()
        self.store = store or MemoryStore(self.config)
        self.retriever = MemoryRetriever(self.store, self.config)

    def _scope_for_file(self, file_path: Path) -> MemoryScope:
        """Classify known home-level legacy stores as global memory."""
        resolved = file_path.resolve()
        global_roots = [
            self.config.home_dir / ".agent-memory",
            self.config.home_dir / ".claude" / "memory",
            self.config.home_dir / ".claude-mem",
            self.config.home_dir / ".claude" / "projects",
        ]
        for root in global_roots:
            try:
                resolved.relative_to(root.resolve())
                return MemoryScope.GLOBAL
            except ValueError:
                continue
        return MemoryScope.PROJECT

    @staticmethod
    def _classify_type(text: str) -> MemoryType:
        lowered = _fold(text)
        for mtype, keywords in FOLDED_TYPE_KEYWORDS:
            for keyword in keywords:
                if keyword in lowered:
                    return mtype
        return MemoryType.PREFERENCE

    @staticmethod
    def _claude_memory_type(frontmatter_type: str) -> Optional[MemoryType]:
        """Map Claude Code auto-memory types onto m3m types."""
        mapping = {
            "user": MemoryType.PREFERENCE,
            "feedback": MemoryType.LESSON,
            "project": MemoryType.PROJECT_FACT,
            "reference": MemoryType.PROJECT_FACT,
        }
        return mapping.get(str(frontmatter_type).strip().lower())

    def scan_sources(self) -> List[Tuple[str, Path, int]]:
        """Scan system for importable legacy memory sources."""
        sources: List[Tuple[str, Path, int]] = []
        home = self.config.home_dir

        def add(label: str, path: Path) -> None:
            if path == self.config.global_dir or not path.is_dir():
                return
            if "sessions" in path.parts or ".index" in path.parts:
                return
            files = [
                f for f in path.glob("**/*")
                if f.is_file() and f.suffix.lower() in {".md", ".mdc", ".json"}
                and not f.name.startswith(".")
            ]
            if files:
                sources.append((label, path, len(files)))

        # 1. Legacy agent memory directory
        add("Legacy agent memory (~/.agent-memory)", home / ".agent-memory")

        # 2. Legacy Claude memory directories
        add("Legacy Claude memory (~/.claude/memory)", home / ".claude" / "memory")

        # 2b. Legacy agent memory store
        add("Legacy agent memory store (~/.claude-mem)", home / ".claude-mem")

        # 2c. Legacy SQLite stores
        for pattern in ("**/*.db", "**/*.sqlite", "**/*.sqlite3"):
            for db_file in (home / ".claude-mem").glob(pattern):
                if db_file.is_file() and not db_file.name.startswith("."):
                    sources.append((f"Legacy SQLite memory store ({db_file.name})", db_file, 1))

        # 3. Claude Code native auto memory (per-project MEMORY.md + topic files)
        projects_root = home / ".claude" / "projects"
        if projects_root.is_dir():
            for memory_dir in sorted(projects_root.glob("*/memory")):
                add(f"Claude Code memory ({memory_dir.parent.name})", memory_dir)

        # 4. Project steering files in the current working directory
        cwd = Path.cwd()
        for steering_name in ["CLAUDE.md", "AGENTS.md", ".cursorrules", ".windsurfrules", ".clinerules"]:
            st_file = cwd / steering_name
            if st_file.is_file():
                sources.append((f"Project file ({steering_name})", st_file, 1))

        for rules_dir_name in [".cursor/rules", ".windsurf/rules", ".clinerules", ".agent/rules"]:
            rules_dir = cwd / rules_dir_name
            if rules_dir.is_dir():
                files = [
                    f for f in rules_dir.glob("**/*")
                    if f.is_file() and f.suffix.lower() in {".md", ".mdc"}
                ]
                if files:
                    sources.append((f"Project rules ({rules_dir_name})", rules_dir, len(files)))

        return sources

    def _is_claude_memory_dir(self, source_path: Path) -> bool:
        """Whether the path is a Claude Code per-project memory directory."""
        projects_root = (self.config.home_dir / ".claude" / "projects").resolve()
        try:
            resolved = source_path.resolve()
            resolved.relative_to(projects_root)
        except ValueError:
            return False
        return resolved.name == "memory" and resolved.parent.name.startswith("-")

    def _claude_memory_route(self, source_path: Path) -> Optional[Path]:
        """Return the real project root encoded in a Claude Code memory dir.

        Claude Code stores per-project auto memory under
        ``~/.claude/projects/<encoded-path>/memory`` where ``<encoded-path>``
        is the project path with ``/`` replaced by ``-``. Returns None when
        the encoded path does not exist on disk (callers fall back to global
        scope); use ``_is_claude_memory_dir`` to distinguish that from a
        non-Claude source.
        """
        if not self._is_claude_memory_dir(source_path):
            return None
        encoded = source_path.resolve().parent.name
        return self._decode_project_path(encoded)

    def _decode_project_path(self, encoded: str) -> Optional[Path]:
        """Rebuild a dash-encoded absolute path against the real filesystem.

        Literal dashes inside component names (e.g. ``acme-infra``) are
        ambiguous after encoding, so candidate paths are verified with
        backtracking: a prefix is only kept while the directory exists.
        """
        tokens = encoded[1:].split("-")
        if not tokens or not all(tokens):
            return None

        def search(index: int, parts: List[str]) -> Optional[Path]:
            if index == len(tokens):
                path = Path("/" + "/".join(parts))
                return path if path.is_dir() else None
            # Option 1: this token continues the previous component (literal dash).
            if parts:
                merged = parts[:-1] + [parts[-1] + "-" + tokens[index]]
                if Path("/" + "/".join(merged)).is_dir():
                    found = search(index + 1, merged)
                    if found is not None:
                        return found
            # Option 2: start a new component, absorbing literal dashes greedily.
            for take in range(len(tokens) - index, 0, -1):
                component = "-".join(tokens[index:index + take])
                extended = parts + [component]
                if Path("/" + "/".join(extended)).is_dir():
                    found = search(index + take, extended)
                    if found is not None:
                        return found
            return None

        return search(0, [])

    def describe_route(self, source_path: Path) -> str:
        """Human-readable import target for a source (used by --dry-run)."""
        if not self._is_claude_memory_dir(source_path):
            return ""
        route = self._claude_memory_route(source_path)
        if route is None:
            return "global (path not found on disk)"
        if self.config.project_root and route.resolve() == self.config.project_root.resolve():
            return "this project"
        if route.is_dir():
            return f"project {route.name}"
        return "global (path not found on disk)"

    def import_from_source(self, source_path: Path, dry_run: bool = False) -> List[MemoryItem]:
        """Import memories from a file or folder source into m3m."""
        # Claude Code memory dirs encode the owning project. Memories from
        # *other* existing projects are routed into those projects' own
        # `.agent-m3m/` stores instead of polluting global memory.
        route = self._claude_memory_route(source_path)
        if (
            route is not None
            and route.is_dir()
            and self.config.project_root
            and route.resolve() != self.config.project_root.resolve()
        ):
            routed_config = Config(
                global_path=self.config.global_dir,
                project_path=route / ".agent-m3m",
            )
            routed_importer = MemoryImporter(
                store=MemoryStore(routed_config),
                config=routed_config,
            )
            return routed_importer.import_from_source(source_path, dry_run=dry_run)

        # A Claude memory dir decoded to *this* project keeps project scope
        # even though the raw directory lives under ~/.claude.
        force_project_scope = (
            route is not None
            and route.is_dir()
            and self.config.project_root is not None
            and route.resolve() == self.config.project_root.resolve()
        )

        imported: List[MemoryItem] = []

        if source_path.is_file():
            if source_path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                imported.extend(self._parse_sqlite(source_path))
            else:
                imported.extend(self._parse_file(source_path))
        elif source_path.is_dir():
            for file_path in sorted(source_path.glob("**/*")):
                if not file_path.is_file():
                    continue
                if (
                    file_path.name.startswith(".")
                    or "sessions" in file_path.parts
                    or ".index" in file_path.parts
                ):
                    continue
                if file_path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                    imported.extend(self._parse_sqlite(file_path))
                elif file_path.suffix.lower() in {".md", ".mdc", ".json"}:
                    imported.extend(self._parse_file(file_path))

        cleaned: List[MemoryItem] = []
        for item in imported:
            item.content = scrub_secrets(item.content).strip()
            if item.content:
                cleaned.append(item)
        imported = cleaned[:MAX_ITEMS_PER_FILE]

        if dry_run:
            if force_project_scope:
                for item in imported:
                    item.scope = MemoryScope.PROJECT
            return imported

        if force_project_scope:
            for item in imported:
                item.scope = MemoryScope.PROJECT

        existing = self.store.list_items(include_expired=True)
        fingerprints = {
            (item.scope.value if isinstance(item.scope, MemoryScope) else str(item.scope),
             item.source,
             " ".join(item.content.lower().split()))
            for item in existing
        }
        content_fingerprints = {
            (item.scope.value if isinstance(item.scope, MemoryScope) else str(item.scope),
             " ".join(item.content.lower().split()))
            for item in existing
        }
        saved_items = []
        # Fuzzy near-duplicate rejection: paraphrased imports must not pile up
        # next to the memories they duplicate. Compared against the existing
        # store AND against items saved earlier in this same batch.
        existing_tokens = {
            item.id: meaningful_tokens(item.content)
            for item in existing
        }
        existing_pool = [(item.scope, existing_tokens[item.id]) for item in existing]
        batch_tokens: List[Tuple[MemoryScope, set]] = []
        for item in imported:
            item.source = f"import:{source_path.resolve()}"
            item.evidence = [f"Imported from {source_path.resolve()}"]
            item.project_id = self.config.project_id if item.scope == MemoryScope.PROJECT else None
            fingerprint = (
                item.scope.value,
                item.source,
                " ".join(item.content.lower().split()),
            )
            content_fingerprint = (item.scope.value, " ".join(item.content.lower().split()))
            if fingerprint in fingerprints or content_fingerprint in content_fingerprints:
                continue
            item_tokens = meaningful_tokens(item.content)
            if item_tokens and self._near_duplicate(
                item.scope, item_tokens, existing_pool + batch_tokens
            ):
                continue
            saved = self.store.save_item(item)
            saved_items.append(saved)
            fingerprints.add(fingerprint)
            content_fingerprints.add(content_fingerprint)
            batch_tokens.append((item.scope, item_tokens))

        if saved_items:
            self.retriever.reindex()

        return saved_items

    @staticmethod
    def _near_duplicate(scope: MemoryScope, tokens: set, pool: List[Tuple[MemoryScope, set]]) -> bool:
        for pool_scope, pool_tokens in pool:
            if pool_scope != scope or not pool_tokens:
                continue
            if jaccard(tokens, pool_tokens) >= NEAR_DUPLICATE_JACCARD:
                return True
        return False

    def _parse_sqlite(self, db_path: Path) -> List[MemoryItem]:
        """Read legacy SQLite memory stores defensively (read-only).

        Schema-agnostic: any table with a recognizable text column
        (content/observation/summary/memory/text/fact) yields one memory per
        row. Project and timestamp columns become tags when present.
        """
        items: List[MemoryItem] = []
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return []
        try:
            tables = [
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if isinstance(row[0], str)
                and re.fullmatch(r"[A-Za-z0-9_]+", row[0])
                and not row[0].startswith("sqlite_")
            ]
            for table in tables:
                columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
                text_columns = [c for c in columns if c.lower() in SQLITE_TEXT_COLUMNS]
                if not text_columns:
                    continue
                project_column = next((c for c in columns if c.lower() in SQLITE_PROJECT_COLUMNS), None)
                created_column = next((c for c in columns if c.lower() in SQLITE_CREATED_COLUMNS), None)
                selected = text_columns + [c for c in (project_column, created_column) if c]
                query = f'SELECT {", ".join(chr(34) + c + chr(34) for c in selected)} FROM "{table}"'
                try:
                    rows = conn.execute(query).fetchall()
                except sqlite3.Error:
                    continue
                for row in rows[:MAX_ITEMS_PER_FILE]:
                    text = next(
                        (str(value).strip() for value in row[:len(text_columns)] if value and str(value).strip()),
                        "",
                    )
                    if not text:
                        continue
                    tags = ["migrated", "legacy-sqlite", table]
                    metadata = row[len(text_columns):]
                    if project_column and len(metadata) > 0 and metadata[0]:
                        tags.append(str(metadata[0]).strip()[:40])
                    items.append(MemoryItem(
                        id=generate_memory_id(),
                        content=scrub_secrets(text)[:MAX_ITEM_CHARS],
                        scope=MemoryScope.GLOBAL,
                        type=self._classify_type(text),
                        status=MemoryStatus.ACTIVE,
                        confidence=0.85,
                        tags=tags[:10],
                        source=f"import:{db_path.resolve()}",
                    ))
                    if len(items) >= MAX_ITEMS_PER_FILE:
                        return items
        except sqlite3.Error:
            return items
        finally:
            conn.close()
        return items

    def _parse_file(self, file_path: Path) -> List[MemoryItem]:
        """Parse raw memory markdown, steering files, or JSON into MemoryItems."""
        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []

        items: List[MemoryItem] = []
        scope = self._scope_for_file(file_path)

        if file_path.suffix.lower() == ".json":
            return self._parse_json(content, file_path, scope)

        # Structured m3m/YAML frontmatter: reuse the standard parser once.
        if content.startswith("---") and self._has_memory_frontmatter(content):
            item = MemoryItem.from_markdown(content, file_path=str(file_path))
            item.id = generate_memory_id()
            item.source = f"import:{file_path.resolve()}"
            items.append(item)
            return items

        # Strip foreign frontmatter (e.g. Cursor .mdc description/globs) but
        # keep parsing the body structurally.
        body = content
        if content.startswith("---"):
            match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
            if match:
                frontmatter_type = self._frontmatter_value(match.group(1), "type")
                body = match.group(2)
                mapped = self._claude_memory_type(frontmatter_type or "")
                items.extend(
                    self._parse_structured(_strip_managed_blocks(body), file_path, scope, default_type=mapped)
                )
                return items

        items.extend(self._parse_structured(_strip_managed_blocks(body), file_path, scope))
        return items

    @staticmethod
    def _has_memory_frontmatter(content: str) -> bool:
        """Detect m3m-style frontmatter worth parsing as a single memory.

        Foreign frontmatter (e.g. Claude Code ``type: feedback`` or Cursor
        ``description``/``globs``) must not hijack the single-memory path.
        """
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not match:
            return False
        keys: dict = {}
        for line in match.group(1).splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                keys[key.strip().lower()] = value.strip().strip("\"'")
        if not keys.keys() & {"id", "scope", "status", "superseded_by", "confidence", "type"}:
            return False
        if "type" in keys and keys["type"] not in {member.value for member in MemoryType}:
            return False
        if "status" in keys and keys["status"] not in {member.value for member in MemoryStatus}:
            return False
        return True

    @staticmethod
    def _frontmatter_value(frontmatter: str, key: str) -> Optional[str]:
        for line in frontmatter.splitlines():
            if ":" in line:
                line_key, value = line.split(":", 1)
                if line_key.strip().lower() == key:
                    return value.strip().strip("\"'")
        return None

    def _parse_json(self, content: str, file_path: Path, scope: MemoryScope) -> List[MemoryItem]:
        items: List[MemoryItem] = []
        try:
            payload = json.loads(content)
        except (ValueError, TypeError):
            return []
        records = payload if isinstance(payload, list) else (
            payload.get("memories") if isinstance(payload, dict) else None
        )
        if not isinstance(records, list):
            return []
        for raw in records[:MAX_ITEMS_PER_FILE]:
            if not isinstance(raw, dict):
                continue
            text = next(
                (str(raw[key]).strip() for key in ("content", "memory", "text", "fact") if str(raw.get(key, "")).strip()),
                "",
            )
            if not text:
                continue
            try:
                scope_value = MemoryScope(str(raw.get("scope", scope.value)))
            except ValueError:
                scope_value = scope
            try:
                mtype = MemoryType(str(raw.get("type", "preference")))
            except ValueError:
                mtype = self._classify_type(text)
            items.append(MemoryItem(
                id=generate_memory_id(),
                content=text[:MAX_ITEM_CHARS],
                scope=scope_value,
                type=mtype,
                status=MemoryStatus.ACTIVE,
                confidence=float(raw.get("confidence", 0.9) or 0.9),
                tags=[str(value) for value in raw.get("tags", []) if value is not None][:10],
                entities=[str(value) for value in raw.get("entities", []) if value is not None][:20],
                source=f"import:{file_path.resolve()}",
            ))
        return items

    def _parse_structured(
        self,
        body: str,
        file_path: Path,
        scope: MemoryScope,
        default_type: Optional[MemoryType] = None,
    ) -> List[MemoryItem]:
        """Structural Markdown parse: bullets, sections, then paragraphs.

        Heading text drives deterministic type classification, so long
        steering files (CLAUDE.md, AGENTS.md, rules) stop being dropped.
        """
        items: List[MemoryItem] = []
        source = f"import:{file_path.resolve()}"
        file_type = self._classify_type(file_path.stem)
        current_type = default_type or file_type
        current_heading = ""

        bullet_items: List[Tuple[MemoryType, str]] = []
        sections: List[Tuple[MemoryType, str, str]] = []
        section_buffer: List[str] = []

        def flush_section() -> None:
            if not section_buffer:
                return
            text = " ".join(line.strip() for line in section_buffer if line.strip()).strip()
            if text:
                sections.append((current_type, current_heading, text[:MAX_ITEM_CHARS]))
            section_buffer.clear()

        for raw_line in body.splitlines():
            heading = HEADING_PATTERN.match(raw_line)
            if heading:
                flush_section()
                current_heading = heading.group(2).strip()
                if default_type:
                    current_type = default_type
                elif current_heading:
                    current_type = self._classify_type(current_heading)
                else:
                    current_type = file_type
                continue

            bullet = BULLET_PATTERN.match(raw_line)
            if bullet:
                flush_section()
                fact = bullet.group(1).strip()
                if 8 <= len(fact):
                    bullet_items.append((current_type, fact[:MAX_ITEM_CHARS]))
                continue

            if raw_line.strip():
                section_buffer.append(raw_line)
                continue

            # Blank line: keep paragraph buffered (paragraphs can span lines).
        flush_section()

        def make_item(mtype: MemoryType, text: str, tags: List[str]) -> MemoryItem:
            return MemoryItem(
                id=generate_memory_id(),
                content=text,
                scope=scope,
                type=mtype,
                status=MemoryStatus.ACTIVE,
                confidence=0.9,
                tags=tags,
                source=source,
            )

        seen: set = set()
        for mtype, fact in bullet_items:
            normalized = " ".join(fact.lower().split())
            if normalized in seen or len(fact) < 8:
                continue
            seen.add(normalized)
            tags = ["migrated", file_path.stem.lower()]
            if mtype is not None and mtype != MemoryType.PREFERENCE:
                tags.append(mtype.value)
            items.append(make_item(mtype, fact, tags))
            if len(items) >= MAX_ITEMS_PER_FILE:
                return items

        for mtype, heading, text in sections:
            normalized = " ".join(text.lower().split())
            if normalized in seen or len(text) < 20:
                continue
            seen.add(normalized)
            tags = ["migrated", file_path.stem.lower()]
            if heading:
                tags.append(heading.lower()[:40].replace(" ", "-"))
            items.append(make_item(mtype, text, tags))
            if len(items) >= MAX_ITEMS_PER_FILE:
                return items

        # No structural content: fall back to whole-file (small) or paragraphs.
        if not items:
            stripped = body.strip()
            if 10 < len(stripped) <= MAX_ITEM_CHARS:
                items.append(make_item(
                    default_type or self._classify_type(file_path.stem),
                    stripped,
                    ["migrated", file_path.stem.lower()],
                ))
            elif len(stripped) > MAX_ITEM_CHARS:
                paragraph: List[str] = []
                for raw_line in stripped.splitlines() + [""]:
                    if raw_line.strip():
                        paragraph.append(raw_line.strip())
                        continue
                    if paragraph:
                        text = " ".join(paragraph)[:MAX_ITEM_CHARS]
                        paragraph = []
                        if 40 <= len(text):
                            items.append(make_item(
                                default_type or file_type,
                                text,
                                ["migrated", file_path.stem.lower()],
                            ))
                    if len(items) >= MAX_ITEMS_PER_FILE:
                        break
        return items

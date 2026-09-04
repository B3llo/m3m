"""Unit tests for agent-m3m."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock
import json
import os
import subprocess
import sys
from pathlib import Path

from agent_m3m.models import MemoryItem, MemoryScope, MemoryType, MemoryStatus, generate_memory_id
from agent_m3m.sanitizer import scrub_secrets, has_secrets
from agent_m3m.config import Config
from agent_m3m.store import MemoryStore
from agent_m3m.retriever import MemoryRetriever
from agent_m3m.importer import MemoryImporter
from agent_m3m.doctor import Doctor
from agent_m3m.extractor import MemoryExtractor
from agent_m3m.hooks_installer import HooksInstaller
from agent_m3m.cli import confirm_archive_all, run_interactive_setup
from agent_m3m.context import ContextBuilder
from agent_m3m.handoff import Handoff, HandoffStore


class TestAgentM3M(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.global_path = Path(self.tmp_dir.name) / "global_memory"
        self.proj_path = Path(self.tmp_dir.name) / "project_memory"

        self.config = Config(global_path=self.global_path, project_path=self.proj_path)
        self.store = MemoryStore(self.config)
        self.retriever = MemoryRetriever(self.store, self.config)

        self.store.init_global()
        self.store.init_project()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_secret_sanitizer(self):
        secret_text = "My secret key is sk-proj-1234567890abcdef1234567890abcdef and token ghp_1234567890abcdef1234567890abcdef"
        self.assertTrue(has_secrets(secret_text))

        clean_text = scrub_secrets(secret_text)
        self.assertNotIn("sk-proj-1234567890abcdef", clean_text)
        self.assertIn("[REDACTED_API_KEY]", clean_text)
        self.assertIn("[REDACTED_GITHUB_TOKEN]", clean_text)

    def test_secret_sanitizer_covers_bearer_and_cloud_tokens(self):
        secret_text = "Authorization: Bearer abcdefghijklmnop AWS_SESSION_TOKEN=abcdefghijklmnopqrstuvwxyz"
        self.assertTrue(has_secrets(secret_text))
        clean_text = scrub_secrets(secret_text)
        self.assertNotIn("abcdefghijklmnop", clean_text)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", clean_text)

    def test_memory_item_frontmatter_roundtrip(self):
        item = MemoryItem(
            id="mem_test_123",
            content="Prefers pnpm over npm for Node.js projects.",
            scope=MemoryScope.GLOBAL,
            type=MemoryType.PREFERENCE,
            status=MemoryStatus.ACTIVE,
            confidence=0.95,
            tags=["javascript", "pnpm"],
            entities=["pnpm", "npm"],
        )
        md = item.to_markdown()
        self.assertIn('id: "mem_test_123"', md)
        self.assertIn('scope: "global"', md)

        reloaded = MemoryItem.from_markdown(md)
        self.assertEqual(reloaded.id, "mem_test_123")
        self.assertEqual(reloaded.content, "Prefers pnpm over npm for Node.js projects.")
        self.assertEqual(reloaded.tags, ["javascript", "pnpm"])
        self.assertEqual(reloaded.entities, ["pnpm", "npm"])

    def test_store_crud(self):
        item1 = MemoryItem(
            id=generate_memory_id(),
            content="Use Terraform modules with local state.",
            scope=MemoryScope.GLOBAL,
            type=MemoryType.LESSON,
        )
        saved = self.store.save_item(item1)
        self.assertTrue(Path(saved.file_path).exists())

        loaded = self.store.load_item_by_id(saved.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.content, "Use Terraform modules with local state.")

    def test_fts5_indexing_and_ranking(self):
        item1 = MemoryItem(
            id="mem_01",
            content="Use pnpm package manager in Node projects.",
            scope=MemoryScope.GLOBAL,
            type=MemoryType.PREFERENCE,
            tags=["pnpm", "node"],
            entities=["pnpm"],
        )
        item2 = MemoryItem(
            id="mem_02",
            content="Docker containers should run as non-root user.",
            scope=MemoryScope.GLOBAL,
            type=MemoryType.LESSON,
            tags=["docker", "security"],
            entities=["docker"],
        )
        self.store.save_item(item1)
        self.store.save_item(item2)

        self.retriever.reindex()

        results = self.retriever.search("pnpm node", top_k=5, debug=True)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].item.id, "mem_01")
        self.assertIn("lexical", results[0].breakdown)

    def test_superseded_status(self):
        old_item = MemoryItem(
            id="mem_old",
            content="Prefers npm package manager.",
            status=MemoryStatus.ACTIVE,
        )
        self.store.save_item(old_item)

        new_item = MemoryItem(
            id="mem_new",
            content="Prefers pnpm package manager.",
            status=MemoryStatus.ACTIVE,
            superseded_by="mem_old",
        )

        # Update old item status
        self.store.update_status("mem_old", MemoryStatus.SUPERSEDED, superseded_by="mem_new")

        old_reloaded = self.store.load_item_by_id("mem_old")
        self.assertEqual(old_reloaded.status, MemoryStatus.SUPERSEDED)
        self.assertEqual(old_reloaded.superseded_by, "mem_new")

    def test_importer_migration(self):
        legacy_file = Path(self.tmp_dir.name) / "CLAUDE.md"
        legacy_file.write_text(
            "# Guidelines\n- Always use pnpm instead of npm.\n- Prefer local PostgreSQL database.\n",
            encoding="utf-8",
        )

        importer = MemoryImporter(self.store, self.config)
        imported = importer.import_from_source(legacy_file)
        self.assertEqual(len(imported), 2)
        self.assertIn("pnpm", imported[0].content)

    def test_doctor_diagnostics(self):
        doc = Doctor(self.config)
        diag = doc.run_diagnostics()
        self.assertTrue(len(diag) >= 5)

    def test_extractor_deduplicates_active_memory(self):
        existing = MemoryItem(
            id="mem_existing",
            content="Never use the legacy wrapper in this project.",
            scope=MemoryScope.PROJECT,
            type=MemoryType.CONVENTION,
        )
        self.store.save_item(existing)

        class FailingRunner:
            def run_prompt(self, *_args, **_kwargs):
                return False, "unavailable"

        extractor = MemoryExtractor(
            store=self.store,
            retriever=self.retriever,
            llm_runner=FailingRunner(),
            config=self.config,
        )
        extractor._heuristic_fallback_extraction = lambda _text: [
            MemoryItem(
                id="mem_duplicate",
                content="Never use the legacy wrapper in this project.",
                scope=MemoryScope.PROJECT,
                type=MemoryType.CONVENTION,
            )
        ]
        self.assertEqual(extractor.extract_from_transcript("anything"), [])

    def test_codex_hook_commands_emit_valid_json(self):
        env = os.environ.copy()
        env["HOME"] = self.tmp_dir.name
        env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")

        # Keep subprocess discovery inside the test-owned HOME. Running from
        # the repository would discover its real .agent-m3m queue and could
        # detach a worker that outlives this temporary directory.
        context = subprocess.run(
            [sys.executable, "-m", "agent_m3m.cli", "context", "--hook-output"],
            capture_output=True,
            text=True,
            env=env,
            cwd=self.tmp_dir.name,
            check=True,
        )
        context_json = json.loads(context.stdout)
        self.assertEqual(context_json["hookSpecificOutput"]["hookEventName"], "SessionStart")

        prompt_context = subprocess.run(
            [sys.executable, "-m", "agent_m3m.cli", "context", "--hook-output", "--hook-event", "UserPromptSubmit"],
            input=json.dumps({"prompt": "search migration"}),
            capture_output=True,
            text=True,
            env=env,
            cwd=self.tmp_dir.name,
            check=True,
        )
        self.assertEqual(
            json.loads(prompt_context.stdout)["hookSpecificOutput"]["hookEventName"],
            "UserPromptSubmit",
        )

        stop = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_m3m.cli",
                "save",
                "--stdin",
                "--session-end",
                "--hook-output",
                "--quiet",
            ],
            input="",
            capture_output=True,
            text=True,
            env=env,
            cwd=self.tmp_dir.name,
            check=True,
        )
        self.assertEqual(json.loads(stop.stdout), {"continue": True, "suppressOutput": True})

    def test_codex_stop_rollout_hook_enqueues_without_inline_extraction(self):
        rollout = "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "stop-session"}}),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "Keep the target-local plan."}
                            ],
                        },
                    }
                ),
            ]
        )
        with mock.patch("agent_m3m.cli.Config", return_value=self.config), \
             mock.patch("agent_m3m.cli.MemoryExtractor") as extractor_class, \
             mock.patch("agent_m3m.cli.SessionQueue") as queue_class, \
             mock.patch("agent_m3m.cli._spawn_queue_worker") as spawn_worker, \
             mock.patch.object(sys, "argv", ["m3m", "save", "--stdin", "--hook-output"]), \
             mock.patch.object(sys, "stdin", io.StringIO(rollout)):
            entry = queue_class.return_value.enqueue.return_value
            entry.job_id = "hook_test_job"
            output = io.StringIO()
            with redirect_stdout(output):
                from agent_m3m.cli import main
                main()

        # Hooks must never run LLM extraction inline: the transcript is
        # enqueued for the bounded single-flight background worker instead.
        extractor_class.assert_not_called()
        queue_class.return_value.enqueue.assert_called_once()
        call = queue_class.return_value.enqueue.call_args
        self.assertEqual(call.kwargs.get("raw_input"), rollout)
        spawn_worker.assert_called_once()
        self.assertEqual(
            output.getvalue().strip(),
            json.dumps({"continue": True, "suppressOutput": True}),
        )

    def test_codex_installer_uses_stop_and_preserves_session_end_siblings(self):
        home = Path(self.tmp_dir.name) / "home"
        codex_dir = home / ".codex"
        codex_dir.mkdir(parents=True)
        legacy = {
            "hooks": {
                "SessionEnd": [
                    {
                        "matcher": "",
                        "hooks": [
                            {"type": "command", "command": "m3m save --stdin --session-end --hook-output"},
                            {"type": "command", "command": "keep-session-end-hook"},
                        ],
                    }
                ],
                "Stop": [
                    {
                        "matcher": "",
                        "hooks": [
                            {"type": "command", "command": "m3m save --stdin"},
                            {"type": "command", "command": "keep-stop-hook"},
                        ],
                    }
                ],
            }
        }
        (codex_dir / "hooks.json").write_text(json.dumps(legacy), encoding="utf-8")
        config_file = codex_dir / "config.toml"
        config_file.write_text("[hooks.state]\nenabled = false\n", encoding="utf-8")

        _, _, ok = HooksInstaller(home_dir=home).install_codex_hooks()

        self.assertTrue(ok)
        installed = json.loads((codex_dir / "hooks.json").read_text(encoding="utf-8"))
        session_end_commands = [
            hook["command"]
            for entry in installed["hooks"]["SessionEnd"]
            for hook in entry.get("hooks", [])
            if hook.get("type") == "command"
        ]
        stop_commands = [
            hook["command"]
            for entry in installed["hooks"]["Stop"]
            for hook in entry.get("hooks", [])
            if hook.get("type") == "command"
        ]
        self.assertIn("keep-session-end-hook", session_end_commands)
        self.assertNotIn("m3m save --stdin --session-end --hook-output", session_end_commands)
        self.assertIn("keep-stop-hook", stop_commands)
        self.assertEqual(
            [command for command in stop_commands if command.startswith("m3m save")],
            ["m3m save --stdin --session-end --hook-output --quiet"],
        )
        self.assertEqual(config_file.read_text(encoding="utf-8"), "[hooks.state]\nenabled = false\n")

    def test_installer_replaces_legacy_codex_hook_commands(self):
        home = Path(self.tmp_dir.name) / "home"
        codex_dir = home / ".codex"
        codex_dir.mkdir(parents=True)
        legacy = {
            "hooks": {
                "SessionStart": [{"type": "command", "command": "m3m context"}],
                "Stop": [
                    {
                        "matcher": "",
                        "hooks": [
                            {"type": "command", "command": "m3m save --stdin"},
                            {"type": "command", "command": "keep-stop-hook"},
                        ],
                    }
                ],
                "SessionEnd": [
                    {
                        "matcher": "",
                        "hooks": [
                            {"type": "command", "command": "m3m save --stdin --quiet"},
                            {"type": "command", "command": "keep-session-end-hook"},
                        ],
                    }
                ],
            }
        }
        (codex_dir / "hooks.json").write_text(json.dumps(legacy), encoding="utf-8")

        _, _, ok = HooksInstaller(home_dir=home).install_codex_hooks()
        self.assertTrue(ok)
        installed = json.loads((codex_dir / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(len(installed["hooks"]["SessionStart"]), 1)
        self.assertIn("--hook-output", str(installed["hooks"]["SessionStart"]))
        stop_commands = [
            hook["command"]
            for entry in installed["hooks"]["Stop"]
            for hook in entry.get("hooks", [])
            if hook.get("type") == "command"
        ]
        self.assertIn("keep-stop-hook", stop_commands)
        self.assertEqual(
            [command for command in stop_commands if command.startswith("m3m save")],
            ["m3m save --stdin --session-end --hook-output --quiet"],
        )
        session_end_commands = [
            hook["command"]
            for entry in installed["hooks"]["SessionEnd"]
            for hook in entry.get("hooks", [])
            if hook.get("type") == "command"
        ]
        self.assertIn("keep-session-end-hook", session_end_commands)
        self.assertEqual(
            [command for command in session_end_commands if command.startswith("m3m save")],
            [],
        )

    def test_opencode_installer_writes_current_esm_harness(self):
        home = Path(self.tmp_dir.name) / "home"

        name, _, ok = HooksInstaller(home_dir=home).install_opencode_hooks()

        self.assertTrue(ok)
        self.assertEqual(name, "OpenCode Plugin")
        plugin = (home / ".config" / "opencode" / "plugins" / "m3m-memory.js").read_text(encoding="utf-8")
        self.assertIn('import { spawn } from "node:child_process"', plugin)
        self.assertIn("experimental.chat.system.transform", plugin)
        self.assertIn('"chat.message"', plugin)
        self.assertIn('event.type === "session.idle"', plugin)
        self.assertIn("client.session.messages", plugin)
        self.assertIn("<m3m-persistent-memory>", plugin)
        self.assertNotIn("client.session.prompt", plugin)
        self.assertNotIn('event.type === "session.created"', plugin)
        self.assertNotIn("session:start", plugin)
        self.assertNotIn("module.exports", plugin)

    def test_opencode_plugin_template_matches_repo_harness(self):
        repo_plugin = (Path(__file__).resolve().parent.parent / "hooks" / "opencode_plugin.js").read_text(encoding="utf-8")
        home = Path(self.tmp_dir.name) / "home"

        HooksInstaller(home_dir=home).install_opencode_hooks()

        installed = (home / ".config" / "opencode" / "plugins" / "m3m-memory.js").read_text(encoding="utf-8")
        self.assertEqual(repo_plugin, installed)

    def test_forget_all_requires_two_confirmations_then_archives_active_memories(self):
        self.store.save_item(MemoryItem(id="mem_global", content="global", scope=MemoryScope.GLOBAL))
        self.store.save_item(MemoryItem(id="mem_project", content="project", scope=MemoryScope.PROJECT))

        inputs = iter(["ARCHIVE ALL", "CONFIRM"])
        with mock.patch("builtins.input", side_effect=lambda _prompt: next(inputs)):
            self.assertTrue(confirm_archive_all(self.store))
        self.assertEqual(self.store.archive_all_active(), 2)
        self.assertEqual(self.store.list_items(status=MemoryStatus.ACTIVE), [])

    def test_forget_all_cancels_on_first_confirmation(self):
        self.store.save_item(MemoryItem(id="mem_active", content="active"))
        with mock.patch("builtins.input", return_value="no"):
            self.assertFalse(confirm_archive_all(self.store))
        self.assertEqual(len(self.store.list_items(status=MemoryStatus.ACTIVE)), 1)

    def test_interactive_setup_does_not_import_legacy_memories(self):
        inputs = iter(["1", "", ""])
        with mock.patch("builtins.input", side_effect=lambda _prompt: next(inputs)), \
             mock.patch("agent_m3m.cli.MemoryImporter") as importer:
            run_interactive_setup(self.config, self.store)
        importer.assert_not_called()

    def test_importer_preserves_legacy_source_content_without_special_cases(self):
        legacy_dir = Path(self.tmp_dir.name) / "legacy"
        legacy_dir.mkdir()
        (legacy_dir / "CORE.md").write_text(
            "# Global Core Memory\n\n- Prefers pnpm over npm for Node.js projects.\n",
            encoding="utf-8",
        )
        (legacy_dir / "sample.md").write_text(
            "---\nid: \"sample\"\nscope: \"global\"\ntype: \"preference\"\nstatus: \"active\"\n---\n\nAlways prefer pnpm over npm or yarn in JS projects.\n",
            encoding="utf-8",
        )
        (legacy_dir / "real.md").write_text(
            "---\nid: \"real\"\nscope: \"global\"\ntype: \"preference\"\nstatus: \"active\"\n---\n\nUse uv for Python tooling.\n",
            encoding="utf-8",
        )

        imported = MemoryImporter(self.store, self.config).import_from_source(legacy_dir)
        self.assertCountEqual(
            [item.content for item in imported],
            [
                "Prefers pnpm over npm for Node.js projects.",
                "Always prefer pnpm over npm or yarn in JS projects.",
                "Use uv for Python tooling.",
            ],
        )

    def test_importer_is_idempotent(self):
        legacy_file = Path(self.tmp_dir.name) / "repeat.md"
        legacy_file.write_text("# Rules\n- Always use uv for Python tooling.\n", encoding="utf-8")
        importer = MemoryImporter(self.store, self.config)
        self.assertEqual(len(importer.import_from_source(legacy_file)), 1)
        self.assertEqual(len(importer.import_from_source(legacy_file)), 0)

    def test_frontmatter_roundtrip_preserves_commas_and_quotes(self):
        item = MemoryItem(
            id="mem_special",
            content="Keep metadata stable.",
            tags=["a,b", 'a"b'],
            entities=["x, y", "quoted \"entity\""],
            evidence=["line, one", "line \"two\""],
        )
        loaded = MemoryItem.from_markdown(item.to_markdown())
        self.assertEqual(loaded.tags, item.tags)
        self.assertEqual(loaded.entities, item.entities)
        self.assertEqual(loaded.evidence, item.evidence)

    def test_search_isolated_by_project_and_scope_filter(self):
        project_item = MemoryItem(
            id="project-only", content="alpha project fact", scope=MemoryScope.PROJECT,
            project_id=self.config.project_id,
        )
        global_item = MemoryItem(id="global-beta", content="beta global fact", scope=MemoryScope.GLOBAL)
        self.store.save_item(project_item)
        self.store.save_item(global_item)
        self.assertEqual(self.retriever.search("alpha", scope="global"), [])
        self.assertEqual(self.retriever.search("alpha", scope="project")[0].item.id, "project-only")

    def test_index_refreshes_after_manual_markdown_edit(self):
        item = self.store.save_item(MemoryItem(id="manual-edit", content="before edit"))
        self.retriever.reindex()
        self.assertEqual(self.retriever.search("before")[0].item.id, "manual-edit")
        Path(item.file_path).write_text(item.to_markdown().replace("before edit", "after edit"), encoding="utf-8")
        self.assertEqual(self.retriever.search("after")[0].item.id, "manual-edit")

    def test_expired_memory_is_not_returned(self):
        expired = MemoryItem(id="expired", content="expired database fact", expires_at="2000-01-01T00:00:00+00:00")
        self.store.save_item(expired)
        self.assertEqual(self.retriever.search("database"), [])

    def test_context_prompt_retrieval_and_handoff(self):
        self.store.save_item(MemoryItem(id="prompt-memory", content="Use the local SQLite migration workflow."))
        HandoffStore(self.config).save(Handoff(session_id="s1", summary="Continue the migration", next_steps=["Run tests"]))
        context = ContextBuilder(self.store, self.retriever, self.config).build_context(prompt="SQLite migration")
        self.assertIn("local SQLite migration workflow", context)
        self.assertIn("Continue the migration", context)
        self.assertIn("Run tests", context)


if __name__ == "__main__":
    unittest.main()

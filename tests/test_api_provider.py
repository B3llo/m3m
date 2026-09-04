"""Tests for the direct API (OpenAI-compatible) LLM provider."""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from agent_m3m.config import Config
from agent_m3m.llm_runner import LLMRunner


def _api_config(tmp_root, **overrides):
    config = Config(
        global_path=tmp_root / "global_memory",
        project_path=tmp_root / "project_memory",
    )
    config.active_provider = "api"
    config.model_name = "glm-5.3-flash"
    config.api_base_url = "https://api.example.test/v4/chat/completions"
    config.api_key_env = "M3M_TEST_API_KEY"
    config.api_key = ""
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _mock_response(payload):
    body = json.dumps(payload).encode("utf-8")
    response = mock.MagicMock()
    response.read.return_value = body
    response.__enter__ = mock.MagicMock(return_value=response)
    response.__exit__ = mock.MagicMock(return_value=False)
    return response


class TestApiProviderConfig(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_api_fields_round_trip_through_config_file(self):
        config = _api_config(self.root)
        config.api_base_url = "https://example.test/api/chat"
        config.api_key_env = "MY_KEY_ENV"
        config.api_key = "explicit-key"
        config.save_config()

        reloaded = Config(
            global_path=config.global_dir,
            project_path=config.project_dir,
        )
        self.assertEqual(reloaded.api_base_url, "https://example.test/api/chat")
        self.assertEqual(reloaded.api_key_env, "MY_KEY_ENV")
        self.assertEqual(reloaded.api_key, "explicit-key")


class TestResolveApiKey(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    def test_explicit_config_key_wins(self):
        config = _api_config(self.root, api_key="explicit-key")
        os.environ["M3M_TEST_API_KEY"] = "env-key"
        self.assertEqual(LLMRunner(config).resolve_api_key(), "explicit-key")

    def test_configured_env_var_is_used(self):
        config = _api_config(self.root)
        os.environ["M3M_TEST_API_KEY"] = "env-key"
        self.assertEqual(LLMRunner(config).resolve_api_key(), "env-key")

    def test_common_alias_fallback(self):
        config = _api_config(self.root)
        os.environ["ZAI_API_KEY"] = "alias-key"
        self.assertEqual(LLMRunner(config).resolve_api_key(), "alias-key")

    def test_missing_key_returns_empty(self):
        config = _api_config(self.root)
        self.assertEqual(LLMRunner(config).resolve_api_key(), "")


class TestRunApi(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.env = mock.patch.dict(os.environ, {"M3M_TEST_API_KEY": "secret-key"}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    def test_successful_call_returns_content(self):
        config = _api_config(self.root)
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["auth"] = request.get_header("Authorization")
            return _mock_response(
                {"choices": [{"message": {"role": "assistant", "content": '{"status":"ok"}'}}]}
            )

        with mock.patch("agent_m3m.llm_runner.urllib.request.urlopen", side_effect=fake_urlopen):
            success, output = LLMRunner(config).run_prompt("health check")

        self.assertTrue(success)
        self.assertEqual(output, '{"status":"ok"}')
        self.assertEqual(captured["url"], config.api_base_url)
        self.assertEqual(captured["payload"]["model"], "glm-5.3-flash")
        self.assertEqual(captured["payload"]["messages"][0]["content"], "health check")
        self.assertFalse(captured["payload"]["stream"])
        self.assertEqual(captured["auth"], "Bearer secret-key")

    def test_markdown_fences_are_stripped(self):
        config = _api_config(self.root)
        fenced = "```json\n{\"memories\": []}\n```"
        with mock.patch(
            "agent_m3m.llm_runner.urllib.request.urlopen",
            side_effect=lambda *_a, **_k: _mock_response(
                {"choices": [{"message": {"content": fenced}}]}
            ),
        ):
            success, output = LLMRunner(config).run_prompt("extract")
        self.assertTrue(success)
        self.assertEqual(output, '{"memories": []}')

    def test_list_content_parts_are_joined(self):
        config = _api_config(self.root)
        with mock.patch(
            "agent_m3m.llm_runner.urllib.request.urlopen",
            side_effect=lambda *_a, **_k: _mock_response(
                {"choices": [{"message": {"content": [{"type": "text", "text": "ok"}]}}]}
            ),
        ):
            success, output = LLMRunner(config).run_prompt("extract")
        self.assertTrue(success)
        self.assertEqual(output, "ok")

    def test_http_error_returns_failure_without_leaking_key(self):
        config = _api_config(self.root)
        error = urllib.error.HTTPError(
            config.api_base_url,
            429,
            "Too Many Requests",
            hdrs=None,
            fp=io.BytesIO(b'{"error":{"message":"quota exceeded"}}'),
        )
        with mock.patch("agent_m3m.llm_runner.urllib.request.urlopen", side_effect=error):
            success, output = LLMRunner(config).run_prompt("extract")
        self.assertFalse(success)
        self.assertIn("429", output)
        self.assertNotIn("secret-key", output)

    def test_error_payload_returns_failure(self):
        config = _api_config(self.root)
        with mock.patch(
            "agent_m3m.llm_runner.urllib.request.urlopen",
            side_effect=lambda *_a, **_k: _mock_response({"error": {"message": "bad model"}}),
        ):
            success, output = LLMRunner(config).run_prompt("extract")
        self.assertFalse(success)
        self.assertIn("bad model", output)

    def test_missing_key_fails_closed(self):
        config = _api_config(self.root)
        os.environ.pop("M3M_TEST_API_KEY", None)
        success, output = LLMRunner(config).run_prompt("extract")
        self.assertFalse(success)
        self.assertIn("M3M_TEST_API_KEY", output)

    def test_missing_model_fails_closed(self):
        config = _api_config(self.root, model_name="")
        success, output = LLMRunner(config).run_prompt("extract")
        self.assertFalse(success)
        self.assertIn("model_name", output)

    def test_codex_aliases_never_reach_the_api_provider(self):
        config = _api_config(self.root, model_name="luna")
        success, output = LLMRunner(config).run_prompt("extract")
        self.assertFalse(success)
        self.assertIn("model_name", output)


class TestVerifyApiProvider(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.env = mock.patch.dict(os.environ, {"M3M_TEST_API_KEY": "secret-key"}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    def test_non_live_verification_passes_when_configured(self):
        config = _api_config(self.root)
        success, message = LLMRunner(config).verify_provider(live=False)
        self.assertTrue(success)
        self.assertIn("glm-5.3-flash", message)

    def test_non_live_verification_fails_without_key(self):
        config = _api_config(self.root)
        os.environ.pop("M3M_TEST_API_KEY", None)
        success, message = LLMRunner(config).verify_provider(live=False)
        self.assertFalse(success)
        self.assertIn("M3M_TEST_API_KEY", message)

    def test_live_verification_runs_health_check(self):
        config = _api_config(self.root)
        with mock.patch(
            "agent_m3m.llm_runner.urllib.request.urlopen",
            side_effect=lambda *_a, **_k: _mock_response(
                {"choices": [{"message": {"content": "{\"status\":\"ok\"}"}}]}
            ),
        ):
            success, message = LLMRunner(config).verify_provider(live=True)
        self.assertTrue(success)
        self.assertIn("live check passed", message)


if __name__ == "__main__":
    unittest.main()

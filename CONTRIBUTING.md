# Contributing to agent-m3m

Thank you for helping improve agent-m3m. The project is preview-environment and treats Markdown memory files as the source of truth, so changes must preserve user data and existing agent integrations.

## Supported development environments

The currently supported operating systems are macOS and Linux with Python 3.9 through 3.13. Windows is not currently supported because process management and file locking rely on POSIX behavior.

## Development setup

Create an isolated environment and install the development tools:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Run the local checks before opening a pull request:

```bash
ruff check src tests
python -m compileall src tests
python -m unittest discover -s tests -v
coverage run --source=agent_m3m -m unittest discover -s tests
coverage report --fail-under=75
```

For OpenCode integration changes, also validate the checked-in harness:

```bash
bun --check hooks/opencode_plugin.js
python -m unittest tests.test_agent_m3m.TestAgentM3M.test_opencode_installer_writes_current_esm_harness
python -m unittest tests.test_agent_m3m.TestAgentM3M.test_opencode_plugin_template_matches_repo_harness
```

## Change guidelines

- Keep changes focused and include regression tests for changed behavior.
- Preserve compatibility with existing Markdown memories and configuration unless the change includes a documented migration.
- Treat hook output as a public protocol. Test installation and runtime behavior for every affected agent.
- Never commit `.agent-m3m/`, transcripts, API keys, local configuration, or real user memories. Use synthetic fixtures.
- Update user-facing documentation and `CHANGELOG.md` when behavior changes.

## Pull requests

Explain the problem, the compatibility impact, and the checks you ran. Small pull requests are easier to review. By contributing, you agree that your contribution is licensed under the repository's MIT License.

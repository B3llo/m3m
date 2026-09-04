# Release process

agent-m3m is currently beta software for macOS and Linux on Python 3.9 through 3.13. Releases must preserve existing Markdown memory files and should not alter installed hooks without explicit, tested behavior.

## Before release

1. Review the working tree for secrets, transcripts, personal memories, generated `.agent-m3m` data, root `AGENTS.md` or `MEMORY.md` exports, and local paths.
2. Confirm the package version is consistent in `pyproject.toml` and `agent_m3m.__version__`.
3. Move user-visible entries from `Unreleased` into a dated version section in `CHANGELOG.md`.
4. Run the complete local checks:

   ```bash
   ruff check src tests
   python -m compileall src tests
   coverage run --source=agent_m3m -m unittest discover -s tests -v
   coverage report --show-missing --fail-under=75
   bun --check hooks/opencode_plugin.js
   ```

5. Build and validate distributions from a clean tree:

   ```bash
   python -m build
   python -m twine check dist/*
   ```

6. Install the wheel into a new virtual environment and run `m3m --help`, `m3m init` in a temporary project, and the relevant hook integration checks.
7. Confirm all GitHub CI jobs pass on the exact release commit.
8. Review dependency and security alerts, open issues marked as release blockers, migration notes, and the supported-version statement in `SECURITY.md`.

## Publish

1. Create a signed annotated tag named `vX.Y.Z` from the verified commit.
2. Build distributions from that tag, not from an uncommitted working tree.
3. Publish through a trusted release environment with short-lived credentials. Do not store a PyPI token in the repository.
4. Create release notes from `CHANGELOG.md` and attach checksums for the source and wheel distributions.

Publishing automation is intentionally not included until the repository identity, package ownership, and trusted publisher configuration are established.

The public repository URL is also intentionally omitted until that identity is established. Do not publish placeholder clone, issue, or source URLs in package metadata or documentation.

## After release

1. Install the published package in clean macOS and Linux environments.
2. Run a non-destructive smoke test against synthetic memories.
3. Verify the release page, package metadata, license, and documentation links.
4. Announce known limitations and begin a new `Unreleased` section if needed.

If verification fails after publishing, stop promotion, document the impact, and publish a forward fix. Do not replace an already published artifact under the same version.

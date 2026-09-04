#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

printf '%s\n' "=== Installing m3m in an isolated user environment ==="

if command -v uv >/dev/null 2>&1; then
    printf '%s\n' "Using uv tool install..."
    uv tool install --force "$PROJECT_DIR"
elif command -v pipx >/dev/null 2>&1; then
    printf '%s\n' "Using pipx install..."
    pipx install --force "$PROJECT_DIR"
else
    printf '%s\n' "Error: install uv or pipx first so m3m can be installed without modifying system Python." >&2
    printf '%s\n' "uv:   https://docs.astral.sh/uv/getting-started/installation/" >&2
    printf '%s\n' "pipx: https://pipx.pypa.io/stable/installation/" >&2
    exit 1
fi

printf '\n%s\n' "Installation complete. No hooks, configuration, or memory stores were changed."
printf '%s\n' "When ready, initialize explicitly with:"
printf '%s\n' "  m3m init --global"
printf '%s\n' "Then verify the environment with:"
printf '%s\n' "  m3m doctor"

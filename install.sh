#!/usr/bin/env bash
set -e

echo "=== Installing agent-memory ==="

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"

mkdir -p "$BIN_DIR"

echo "1. Installing Python package in editable mode..."
pip3 install -e "$PROJECT_DIR" --no-deps 2>/dev/null || {
    echo "pip3 install editable failed, symlinking binary to $BIN_DIR/agent-memory..."
    ln -sf "$PROJECT_DIR/src/agent_memory/cli.py" "$BIN_DIR/agent-memory"
    chmod +x "$BIN_DIR/agent-memory"
}

echo "2. Initializing global memory at ~/.agent-memory..."
python3 -m agent_memory.cli init --global

echo "3. Running system diagnostics..."
python3 -m agent_memory.cli doctor

echo ""
echo "=== Installation Complete! ==="
echo "You can now use 'agent-memory' CLI command from your terminal."

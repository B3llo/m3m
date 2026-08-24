#!/usr/bin/env bash
set -e

echo "=== Installing m3m ==="

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"

mkdir -p "$BIN_DIR"

echo "1. Installing Python package..."
if pip3 install -e "$PROJECT_DIR" --break-system-packages 2>/dev/null; then
    echo "✓ Installed via pip3 in editable mode."
elif pip3 install -e "$PROJECT_DIR" 2>/dev/null; then
    echo "✓ Installed via pip3."
else
    echo "✓ Creating CLI wrapper in $BIN_DIR/m3m..."
    cat << EOF > "$BIN_DIR/m3m"
#!/usr/bin/env bash
export PYTHONPATH="$PROJECT_DIR/src:\$PYTHONPATH"
exec python3 -m agent_m3m.cli "\$@"
EOF
    chmod +x "$BIN_DIR/m3m"
fi

# Create symlink for m3m if needed
if [ -f "$BIN_DIR/agent-m3m" ]; then
    ln -sf "$BIN_DIR/agent-m3m" "$BIN_DIR/m3m"
fi

echo "2. Initializing global memory at ~/.agent-m3m..."
PYTHONPATH="$PROJECT_DIR/src" python3 -m agent_m3m.cli init --global

echo "3. Running system diagnostics..."
PYTHONPATH="$PROJECT_DIR/src" python3 -m agent_m3m.cli doctor

echo ""
echo "=== Installation Complete! ==="
echo "Executable located at: $BIN_DIR/m3m"

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$HOME/.local/bin"
chmod +x "$SCRIPT_DIR/agy-bridge"
cp "$SCRIPT_DIR/agy-bridge" "$HOME/.local/bin/agy-bridge"
ln -sf "$HOME/.local/bin/agy-bridge" "$HOME/.local/bin/ab"

if [ -d "$HOME/.gemini/bin" ]; then
  cp "$SCRIPT_DIR/agy-bridge" "$HOME/.gemini/bin/agy-bridge"
fi
if [ -d "$HOME/.gemini/antigravity/bin" ]; then
  cp "$SCRIPT_DIR/agy-bridge" "$HOME/.gemini/antigravity/bin/agy-bridge"
fi

if [ -f "$SCRIPT_DIR/SKILL.md" ]; then
  mkdir -p "$HOME/.gemini/config/skills/project-session-spawner"
  mkdir -p "$HOME/.claude/skills/project-session-spawner"
  cp "$SCRIPT_DIR/SKILL.md" "$HOME/.gemini/config/skills/project-session-spawner/SKILL.md"
  cp "$SCRIPT_DIR/SKILL.md" "$HOME/.claude/skills/project-session-spawner/SKILL.md"
fi

"$HOME/.local/bin/agy-bridge" install-hooks
"$HOME/.local/bin/agy-bridge" doctor

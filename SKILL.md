---
name: project-session-spawner
description: Universal cross-agent project creator, chat spawner, and zero-terminal full-fidelity session context bus (agy-bridge v0.3) across Antigravity 2.0, Claude Desktop / Claude Code, OpenAI Codex, and WorkBuddy. Use whenever the user asks to continue a task from another IDE, check cross-agent progress, create a project/workspace, launch a new chat in Antigravity or Claude, send messages to open Claude Desktop tabs, or record architectural decisions/dead-ends.
---

# Project & Session Spawner (`agy-bridge v0.3` — Zero-Terminal Edition)

This skill and built-in MCP server (`agy_bridge`) connect **Antigravity 2.0**, **Claude Desktop / Claude Code**, **OpenAI Codex**, and **WorkBuddy** on macOS **without requiring the user to open `Terminal.app`**.

## 1. Zero-Terminal Usage (Inside Chat UI via MCP Tools & Automatic Hooks)

### Automatic Background Hooks (0 Clicks, 0 Git Noise)
- **Tracked `CLAUDE.md` and `AGENTS.md` stay 100% clean** in `git status`.
- On every `SessionStart`, `UserPromptSubmit` / `PreInvocation`, `PostToolUse` (`Edit`/`Write`), `Stop`, and `StopFailure` (rate limit / crash), `agy-bridge` updates the shared SQLite WAL database (`<git-common-dir>/agy-bridge/state.sqlite`), `.agy-bridge/LIVE_STATE.md` (~350 tokens), `HANDOFF_CONTEXT.md`, and `.agy-bridge/sessions/<agent>_<id>.md`.
- `SessionStart` and `UserPromptSubmit`/`PreInvocation` hooks inject `.agy-bridge/LIVE_STATE.md` dynamically into the agent's context **only when state changed from another IDE** (0 extra tokens on mid-session turns when no other IDE touched the repo).

### Built-In MCP Tools (`agy_bridge` MCP Server)
Whenever the user says in chat *"continue from Claude"*, *"продолжи за Claude"*, *"что там делал другой агент"*, *"switch to Antigravity"*, or *"send to open Claude tab"*, call the built-in MCP tools directly (or run `~/.local/bin/ab`):
- `bridge_get_state(project?)` — returns the live ~350-token Progress State, modified files matrix, `git status`, `StopFailure` rate-limit alerts, and recorded decisions/dead-ends.
- `bridge_continue_task(project?, to_agent?, prompt?)` — 1-step failover: auto-detects the latest active or rate-limited project across all IDEs, syncs all 3 context layers, and either returns the recovery context in the current chat (`to_agent="current"`) or launches/messages the target IDE (`to_agent="agy"` or `"claude"`).
- `bridge_send_to_claude(target, message)` — injects a message directly into a live open Claude Desktop / CLI tab via `/tmp/cc-socks/<pid>.sock`.
- `bridge_spawn_agy_chat(project, prompt)` — registers the project in Antigravity 2.0 (Turbo + Allow + Eager) and launches + focuses a new live chat tab.
- `bridge_record_decision(summary, kind="decision"|"dead_end", reason?, project?)` — records an architectural decision or rejected dead-end (`"tried X, failed because Y"`) into `<git-common-dir>/agy-bridge/state.sqlite` so other IDEs never repeat the mistake.
- `bridge_search_history(query, project?)` — runs an instant SQLite `FTS5` full-text search across all unabridged session transcripts of the project.

---

## 2. Short CLI Commands (`ab` / `agy-bridge`)

If invoked via `run_command` or terminal:
```bash
# One-step failover: auto-detect latest active/interrupted session & continue in the other IDE:
ab continue
ab to agy
ab to claude

# Health check of daemon, hooks, MCP servers, and live sockets:
ab doctor

# Check status across all 4 IDEs:
ab status
ab sockets

# Sync all 3 layers (SQLite WAL + .agy-bridge/LIVE_STATE.md + HANDOFF_CONTEXT.md + .agy-bridge/sessions/):
ab sync ~/Documents/my-project

# Create project & spawn Antigravity Turbo chat:
ab spawn-agy ~/Documents/my-project "Read HANDOFF_CONTEXT.md and continue development" --focus

# Send message to open Claude Desktop tab:
ab send-claude ~/Documents/my-project "Read the updated context and continue"
```

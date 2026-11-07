# Project: `agy-bridge` — Universal Cross-Agent Context Bus & Session Orchestrator (`v0.3` Zero-Terminal Edition)

## 1. Overview & Goal
Unified cross-agent context bus and session orchestrator between **Antigravity 2.0 (`v2.29.0+`)**, **Claude Desktop / Claude Code**, **OpenAI Codex**, and **WorkBuddy** on macOS.
Goal: ensure all AI coding IDEs share workspaces and worktrees, maintain **100% lossless awareness** of what every other agent did (including architectural decisions, rejected dead-ends, tool outputs/errors, and uncommitted git diffs), and operate **with zero Terminal required** via invisible lifecycle hooks, dynamic context injection (zero `git status` noise), and a built-in stdio MCP server inside every IDE chat.

## 2. Architecture (`v0.3` Zero-Terminal Edition + SQLite WAL + Built-in MCP Server)
- **Single-File Python 3 Stdlib Binary & MCP Server**: [`./agy-bridge`](./agy-bridge) + short alias `ab` (installed to `~/.local/bin/agy-bridge`, `~/.local/bin/ab`, `~/.gemini/bin/agy-bridge`, `~/.gemini/antigravity/bin/agy-bridge`).
- **Global Skill**: [`./SKILL.md`](./SKILL.md) (synced to `~/.gemini/config/skills/project-session-spawner/SKILL.md` and `~/.claude/skills/project-session-spawner/SKILL.md`).
- **Lifecycle Hooks + MCP Across All IDEs**:
  - **Claude Code / Desktop** (`~/.claude/settings.json` + `~/.claude.json`): `SessionStart`, `UserPromptSubmit`, `PostToolUse` (`Edit|Write|MultiEdit`), `Stop`, `StopFailure` + MCP server `agy_bridge`.
  - **Antigravity 2.0** (`~/.gemini/config/plugins/agy-bridge/`): `SessionStart`, `PreInvocation`, `PostToolUse` (`edit_file|write_file`), `Stop` + `mcp_config.json` + `rules/AGENTS.md`.
  - **OpenAI Codex** (`~/.codex/config.toml`): `[mcp_servers.agy_bridge]`.

### Key Architectural Pillars:
1. **Zero Git Noise (`write_tracked_docs=False` by default)**:
   - Tracked `CLAUDE.md` and `AGENTS.md` files are never dirtied by auto-state blocks.
   - A token-lean (~350-token) progress state is written atomically (`0600`) with secret redaction (`redact_secrets`) to `.agy-bridge/LIVE_STATE.md` and `$(git rev-parse --git-common-dir)/agy-bridge/LIVE_STATE.md` (both excluded via `.git/info/exclude`).
   - Agents receive `LIVE_STATE.md` **dynamically in memory** via `SessionStart` and `UserPromptSubmit` (`additionalContext` in Claude) / `PreInvocation` (`injectSteps.ephemeralMessage` in Antigravity).
   - **Delta Cursor (`cursor_<agent>.sha256`)**: if no other agent modified the project state between turns, mid-session hooks return **0 tokens** and run in `<5ms`.

2. **Shared SQLite WAL Store Across Git Worktrees (`<git-common-dir>/agy-bridge/state.sqlite`)**:
   - SQLite `WAL` mode + `fcntl.flock` (`bridge.lock`) prevent race conditions when two agents work concurrently across worktrees.
   - Tables: `sessions`, `decisions` (`decision` & `dead_end`), `events` (`StopFailure` on rate limit or API crash), and `session_fts` (`FTS5` full-text index across all session transcripts).

3. **Rate-Limit & Crash Capture (`StopFailure` + `PostToolUse` Checkpoints)**:
   - Checkpoints sync immediately after every `Edit`/`Write` (`PostToolUse`), without waiting for the turn to end.
   - When an agent hits a rate limit or API error (`StopFailure`), the hook records the failure state and sends a native macOS Notification.

4. **Zero-Terminal Built-in MCP Server (`agy-bridge mcp`)**:
   - Exposes 6 native MCP tools directly inside Antigravity, Claude, and Codex chats:
     - `bridge_get_state` — live session/file matrix, git status, and architectural decisions.
     - `bridge_continue_task` — 1-step failover from another IDE.
     - `bridge_send_to_claude` — inject messages into live open Claude Desktop tabs via `/tmp/cc-socks/<pid>.sock`.
     - `bridge_spawn_agy_chat` — create/open a project and spawn a live chat in Antigravity 2.0 (Turbo mode).
     - `bridge_record_decision` — record a `decision` or rejected `dead_end` so other agents don't repeat mistakes.
     - `bridge_search_history` — instant `FTS5` search across all project session histories.

5. **3-Layer Lossless Memory**:
   - **Layer 1 (`.agy-bridge/LIVE_STATE.md`, ~350 tokens)**: compact progress snapshot with `HEAD sha`, modified files matrix, `StopFailure` alerts, `decisions`/`dead_ends`, and latest outcomes.
   - **Layer 2 (`HANDOFF_CONTEXT.md`)**: multi-agent context with uncommitted `git diff` and tool/reasoning chains.
   - **Layer 3 (`.agy-bridge/sessions/<agent>_<session_id>.md`)**: 100% unabridged session mirrors from Turn 1 to End.

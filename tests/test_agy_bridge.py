#!/usr/bin/env python3
"""
Comprehensive E2E & Integration Test Suite for `agy-bridge v0.3` (Zero-Terminal Edition).
Tests:
  1. Secret redaction (.env, OpenAI, Anthropic, GitHub, Google, Slack, Bearer tokens)
  2. Git worktrees & uncommitted changes resolution + shared <git-common-dir> SQLite WAL
  3. Zero-git-noise guarantee (no untracked CLAUDE.md/AGENTS.md stubs, legacy auto-block stripping)
  4. SQLite WAL concurrency, architectural decisions/dead-ends, StopFailure events, and FTS5 search
  5. Fast JSONL parser (base64 image stripping, mtime/size cache, Prev/Last turn preservation)
  6. Lifecycle hook contracts (Claude & Antigravity) + SHA-256 delta cursor (0-token mid-session turns)
  7. Built-in stdio MCP server (`agy-bridge mcp`) JSON-RPC 2.0 protocol & ultra-lean schema
  8. Live macOS environment health check (`doctor`, `status --json`, `sockets --json`)
"""

import concurrent.futures
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGY_BRIDGE_BIN = REPO_ROOT / "agy-bridge"

# Dynamically load agy-bridge module for unit + integration testing
loader = importlib.machinery.SourceFileLoader("agy_bridge_mod", str(AGY_BRIDGE_BIN))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
ab = importlib.util.module_from_spec(spec)
loader.exec_module(ab)


class TestAgyBridgeV03(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="agy_bridge_test_")).resolve()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _init_git_repo(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
        (path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        (path / "src").mkdir(exist_ok=True)
        (path / "src" / "app.py").write_text("print('v1')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(path), "add", "."], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "initial commit"], check=True)

    def test_01_secret_redaction(self) -> None:
        raw = "\n".join([
            "OPENAI_API_KEY='" + "sk-proj-" + "1234567890abcdefghijklmnop'",
            "ANTHROPIC_Key = " + "sk-ant-" + "api03-abcdef1234567890abcdef1234567890",
            "GITHUB_TOKEN=" + "ghp_" + "1234567890abcdefghijklmnopqrstuvwxyz",
            "PAT=" + "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz1234567890",
            "GOOGLE=" + "AIza" + "SyA1234567890abcdefghijklmnopqrstuvwx",
            "SLACK=" + "xoxb-" + "123456789012-123456789012-abcdefghijklmnopqrstuvwx",
            "Authorization: Bearer " + "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret.sig",
            "DB_PASSWORD=\"super_secret_db_pass_2026\"",
        ])
        redacted = ab.redact_secrets(raw)
        self.assertNotIn("1234567890abcdefghijklmnop", redacted)
        self.assertNotIn("api03-abcdef1234567890", redacted)
        self.assertNotIn("1234567890abcdefghijklmnopqrstuvwxyz", redacted)
        self.assertNotIn("11ABCDEFG0123456789", redacted)
        self.assertNotIn("SyA1234567890abcdefghijklmnopqrstuvwx", redacted)
        self.assertNotIn("123456789012-123456789012", redacted)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", redacted)
        self.assertNotIn("super_secret_db_pass_2026", redacted)
        self.assertIn("[REDACTED", redacted)

    def test_02_git_worktrees_and_uncommitted_diff(self) -> None:
        base_repo = self.tmp_dir / "wt-project"
        self._init_git_repo(base_repo)

        wt_path = base_repo / ".claude" / "worktrees" / "feature-wt"
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "-C", str(base_repo), "worktree", "add", "-q", "-b", "claude/feature-wt", str(wt_path)],
            check=True,
        )

        # Ensure worktree has newer uncommitted changes than base_repo (and no CLAUDE.md!)
        time.sleep(0.05)
        (wt_path / "src" / "app.py").write_text("print('v2 edited in worktree')\n", encoding="utf-8")
        (wt_path / "src" / "new_feature.py").write_text("x = 42\n", encoding="utf-8")

        found_wts = ab.find_worktrees_for_repo(base_repo)
        self.assertIn(wt_path.resolve(), found_wts)

        res_base, res_active, all_paths = ab.resolve_repo_and_worktree(base_repo)
        self.assertEqual(res_base, base_repo.resolve())
        self.assertEqual(res_active, wt_path.resolve())
        self.assertIn(base_repo.resolve(), all_paths)
        self.assertIn(wt_path.resolve(), all_paths)

        # Verify shared <git-common-dir>/agy-bridge directory is identical for base_repo and worktree
        bdir_base = ab.get_git_common_bridge_dir(base_repo)
        bdir_wt = ab.get_git_common_bridge_dir(wt_path)
        self.assertEqual(bdir_base.resolve(), bdir_wt.resolve())

        # Build auto state block and verify multi-worktree matrix is rendered
        block = ab.build_auto_state_block("wt-project", res_active, "test-agy-id", [], [])
        self.assertIn("Live Git & Worktrees State", block)
        self.assertIn("[ACTIVE WORKTREE]", block)
        self.assertIn("[BASE REPO]", block)
        self.assertIn("2 uncommitted file(s)", block)

    def test_03_zero_git_noise_and_legacy_cleanup(self) -> None:
        repo = self.tmp_dir / "clean-repo"
        self._init_git_repo(repo)

        # 1. Syncing a repo without CLAUDE.md / AGENTS.md must NOT create untracked CLAUDE.md / AGENTS.md
        ab.sync_claude_and_agents_md([repo], "clean-repo", auto_state_block="<!-- test -->", write_tracked_docs=False)
        self.assertFalse((repo / "CLAUDE.md").exists())
        self.assertFalse((repo / "AGENTS.md").exists())
        self.assertTrue((repo / ".agy-bridge" / "LIVE_STATE.md").exists())
        _, st_out, _ = ab.run_git(repo, ["status", "-s"])
        self.assertEqual(st_out.strip(), "")

        # 2. Untracked auto-generated boilerplate stub from older version must be automatically removed
        (repo / "CLAUDE.md").write_text(
            f"# clean-repo\n\n{ab.AUTO_BOILERPLATE_MARKER}, **Codex**.\n",
            encoding="utf-8",
        )
        ab.sync_claude_and_agents_md([repo], "clean-repo", auto_state_block="<!-- test -->", write_tracked_docs=False)
        self.assertFalse((repo / "CLAUDE.md").exists())
        _, st_out2, _ = ab.run_git(repo, ["status", "-s"])
        self.assertEqual(st_out2.strip(), "")

        # 3. Tracked human CLAUDE.md with legacy AUTO-STATE block must have the auto-block stripped cleanly
        human_content = "# Human Project Architecture\n\nAlways run pytest before committing.\n"
        (repo / "CLAUDE.md").write_text(human_content, encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "CLAUDE.md"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "add CLAUDE.md"], check=True)

        # Simulate legacy v0.2 dirtying CLAUDE.md with AUTO-STATE block
        dirty_content = human_content + f"\n{ab.AUTO_STATE_START}\n## Old State\n{ab.AUTO_STATE_END}\n"
        (repo / "CLAUDE.md").write_text(dirty_content, encoding="utf-8")
        _, st_dirty, _ = ab.run_git(repo, ["status", "-s"])
        self.assertIn("CLAUDE.md", st_dirty)

        # Syncing with write_tracked_docs=False restores CLAUDE.md to clean git status and doesn't invent AGENTS.md
        ab.sync_claude_and_agents_md([repo], "clean-repo", auto_state_block="<!-- test -->", write_tracked_docs=False)
        self.assertFalse((repo / "AGENTS.md").exists())
        _, st_clean, _ = ab.run_git(repo, ["status", "-s"])
        self.assertEqual(st_clean.strip(), "")

    def test_04_sqlite_wal_decisions_events_and_fts5_concurrency(self) -> None:
        repo = self.tmp_dir / "sqlite-repo"
        self._init_git_repo(repo)

        def _worker(i: int) -> None:
            with ab.with_repo_lock(repo):
                ab.record_repo_decision(
                    repo,
                    kind="dead_end" if i % 2 == 0 else "decision",
                    summary=f"Worker {i} tested approach {i} with secret " + "sk-ant-" + "api03-1234567890abcdef1234567890",
                    reason=f"Reason {i}",
                    agent="claude",
                )
                ab.record_repo_event(
                    repo,
                    event_type="StopFailure",
                    agent="claude",
                    session_id=f"sess-{i:04d}",
                    cwd=str(repo),
                    payload={"error": "rate_limit_exceeded"},
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(_worker, range(12)))

        decisions = ab.get_repo_decisions(repo, limit=20)
        self.assertEqual(len(decisions), 12)
        for d in decisions:
            self.assertNotIn("sk-ant-api03", d["summary"])
            self.assertIn("[REDACTED", d["summary"])

        interrupted = ab.get_recent_interrupted_events(repo, limit=5)
        self.assertEqual(len(interrupted), 5)

        fake_sessions = [
            {
                "agent": "claude",
                "sessionId": "fts-session-1",
                "title": "Fix Redis Connection Pool",
                "cwd": str(repo),
                "branch": "main",
                "mtime": time.time(),
                "lastModified": ab.now_iso(),
                "initialUserGoal": "Configure Redis Sentinel failover",
                "allTurns": [
                    {"role": "user", "text": "Configure Redis Sentinel failover", "timestamp": ab.now_iso()},
                    {"role": "assistant", "text": "Implemented Sentinel backoff in src/redis_pool.py", "timestamp": ab.now_iso()},
                ],
                "modifiedFiles": [str(repo / "src" / "redis_pool.py")],
                "readFiles": [],
            }
        ]
        ab.persist_sessions_to_sqlite(repo, fake_sessions)
        hits = ab.search_repo_history_fts(repo, "Sentinel")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["session_id"], "fts-session-1")

    def test_05_jsonl_parser_base64_stripping_and_cache(self) -> None:
        repo = self.tmp_dir / "parser-repo"
        self._init_git_repo(repo)
        jsonl_file = self.tmp_dir / "test-session-uuid.jsonl"
        fake_b64 = "A" * 120_000  # 120 KB base64 blob
        records = [
            {
                "type": "user",
                "cwd": str(repo),
                "gitBranch": "main",
                "timestamp": "2026-11-07T10:00:00Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "Implement SeedShopService rotation"}]},
            },
            {
                "type": "assistant",
                "timestamp": "2026-11-07T10:01:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "Checking SeedShop rotation math."},
                        {"type": "tool_use", "name": "Edit", "input": {"file_path": str(repo / "src" / "app.py")}},
                        {"type": "text", "text": "Added SeedShopService with 2-minute restock timer and weather mutations."},
                    ],
                },
            },
            {
                "type": "user",
                "timestamp": "2026-11-07T10:01:05Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "content": f'{{"type":"image","source":{{"type":"base64","data":"{fake_b64}"}}}}',
                        }
                    ],
                },
            },
            {
                "type": "user",
                "timestamp": "2026-11-07T10:02:00Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "через mcp создай в антигравити"}]},
            },
            {
                "type": "assistant",
                "timestamp": "2026-11-07T10:02:05Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Создал новый чат в Antigravity и передал контекст."}],
                },
            },
        ]
        jsonl_file.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")

        p1 = ab.parse_claude_or_workbuddy_jsonl(jsonl_file)
        t0 = time.perf_counter()
        p2 = ab.parse_claude_or_workbuddy_jsonl(jsonl_file)
        cached_ms = (time.perf_counter() - t0) * 1000.0
        self.assertLess(cached_ms, 25.0)
        self.assertEqual(p1["totalTurns"], p2["totalTurns"])

        # Verify base64 blob was stripped before parsing
        full_dump = json.dumps(p1)
        self.assertNotIn(fake_b64[:512], full_dump)
        self.assertIn("[BASE64_IMAGE_OMITTED]", full_dump)

        # Verify build_auto_state_block skips meta-handoff noise ("через mcp создай в антигравити")
        # and surfaces the real technical prompt and outcome!
        block = ab.build_auto_state_block("parser-repo", repo, "test-id", [p1], [])
        self.assertIn("src/app.py", block)
        self.assertIn("Added SeedShopService with 2-minute restock timer", block)
        self.assertIn("Last Prompt**: Implement SeedShopService rotation", block)
        self.assertNotIn("через mcp создай в антигравити", block)
        self.assertNotIn("Создал новый чат в Antigravity", block)

    def test_06_hook_lifecycle_and_delta_cursor(self) -> None:
        repo = self.tmp_dir / "hook-repo"
        self._init_git_repo(repo)
        payload = json.dumps({"cwd": str(repo), "session_id": "hook-test-1"})

        # 1. Claude SessionStart -> must output hookSpecificOutput.additionalContext
        r1 = subprocess.run(
            [sys.executable, str(AGY_BRIDGE_BIN), "hook-sync", "--event", "SessionStart"],
            input=payload,
            capture_output=True,
            text=True,
            check=True,
        )
        out1 = json.loads(r1.stdout)
        self.assertEqual(out1["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("Cross-Agent Live Progress State", out1["hookSpecificOutput"]["additionalContext"])

        # 2. Claude UserPromptSubmit immediately after (no state change) -> must output 0 bytes (0 tokens!)
        r2 = subprocess.run(
            [sys.executable, str(AGY_BRIDGE_BIN), "hook-sync", "--event", "UserPromptSubmit"],
            input=payload,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(r2.stdout.strip(), "")

        # 3. Antigravity PreInvocation (first time Antigravity reads this repo) -> must output injectSteps
        r3 = subprocess.run(
            [sys.executable, str(AGY_BRIDGE_BIN), "hook-sync", "--agy-hook", "--event", "PreInvocation"],
            input=payload,
            capture_output=True,
            text=True,
            check=True,
        )
        out3 = json.loads(r3.stdout)
        self.assertIn("injectSteps", out3)

        # 4. Antigravity PreInvocation (second time, no change) -> must output {} (0 tokens!)
        r4 = subprocess.run(
            [sys.executable, str(AGY_BRIDGE_BIN), "hook-sync", "--agy-hook", "--event", "PreInvocation"],
            input=payload,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(r4.stdout), {})

        # 5. Claude StopFailure -> records rate-limit crash and changes LIVE_STATE.md hash so Antigravity sees it!
        fail_payload = json.dumps({"cwd": str(repo), "session_id": "claude-crash-99", "error": "rate_limit_error"})
        subprocess.run(
            [sys.executable, str(AGY_BRIDGE_BIN), "hook-sync", "--event", "StopFailure"],
            input=fail_payload,
            capture_output=True,
            text=True,
            check=True,
        )
        r5 = subprocess.run(
            [sys.executable, str(AGY_BRIDGE_BIN), "hook-sync", "--agy-hook", "--event", "PreInvocation"],
            input=payload,
            capture_output=True,
            text=True,
            check=True,
        )
        out5 = json.loads(r5.stdout)
        self.assertIn("injectSteps", out5)
        self.assertIn("StopFailure", out5["injectSteps"][0]["ephemeralMessage"])
        self.assertIn("rate_limit_error", out5["injectSteps"][0]["ephemeralMessage"])

    def test_07_mcp_server_jsonrpc_stdio(self) -> None:
        repo = self.tmp_dir / "mcp-repo"
        self._init_git_repo(repo)

        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "bridge_record_decision",
                    "arguments": {
                        "project": str(repo),
                        "kind": "dead_end",
                        "summary": "Tried polling FSEvents without flock",
                        "reason": "Caused race condition on worktree switch",
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "bridge_get_state", "arguments": {"project": str(repo)}},
            },
            {"jsonrpc": "2.0", "id": 5, "method": "unknown/method", "params": {}},
        ]
        stdin_data = "\n".join(json.dumps(r) for r in requests) + "\n"
        proc = subprocess.run(
            [sys.executable, str(AGY_BRIDGE_BIN), "mcp"],
            input=stdin_data,
            capture_output=True,
            text=True,
            check=True,
        )
        responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        # Notification has no id and must not produce a response; so 5 responses expected for ids 1..5
        self.assertEqual([r["id"] for r in responses], [1, 2, 3, 4, 5])

        # Verify ultra-lean MCP schema size (< 2200 chars for all 6 tools)
        tools_json_str = json.dumps(responses[1]["result"]["tools"])
        self.assertEqual(len(responses[1]["result"]["tools"]), 6)
        self.assertLess(len(tools_json_str), 2200)

        # Verify recorded dead-end appears in bridge_get_state output
        state_text = responses[3]["result"]["content"][0]["text"]
        self.assertIn("DEAD-END", state_text)
        self.assertIn("Tried polling FSEvents without flock", state_text)

        # Verify unknown method returns JSON-RPC -32601 error
        self.assertEqual(responses[4]["error"]["code"], -32601)

    def test_08_live_macos_doctor_and_status(self) -> None:
        r_doc = subprocess.run(
            [sys.executable, str(AGY_BRIDGE_BIN), "doctor"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("[OK] Claude Lifecycle Hooks", r_doc.stdout)
        self.assertIn("[OK] Antigravity Hooks", r_doc.stdout)
        self.assertIn("[OK] MCP Server (AGY/Claude)", r_doc.stdout)
        self.assertIn("[OK] MCP Server (Codex)", r_doc.stdout)

        r_st = subprocess.run(
            [sys.executable, str(AGY_BRIDGE_BIN), "status", "-n", "3", "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        st_data = json.loads(r_st.stdout)
        self.assertIn("antigravity", st_data)
        self.assertIn("claude", st_data)
        self.assertIn("codex", st_data)

    def test_09_failover_skips_meta_handoff_stub_and_preserves_technical_outcome(self) -> None:
        claude_session = {
            "agent": "claude",
            "sessionId": "claude-substantive-1111",
            "title": "Build PlotService & Shop UI",
            "mtime": 1700000100.0,
            "initialUserGoal": "Build PlotService and SeedShop UI in Roblox",
            "modifiedFiles": ["src/server/PlotService.luau", "src/client/ShopUI.luau"],
            "allTurns": [
                {"role": "user", "text": "Build PlotService and SeedShop UI in Roblox"},
                {"role": "assistant", "text": "Created PlotService.luau with 6 plots."},
                {"role": "user", "text": "Now add remote events for buying seeds and restock timer"},
                {"role": "assistant", "text": "Implemented BuySeedRemote and 120s restock loop in SeedShopService.luau."},
                {"role": "user", "text": "через mcp создай в антигравити"},
                {"role": "assistant", "text": "Запускаю сессию в Antigravity через MCP..."},
            ],
        }
        agy_meta_stub = {
            "agent": "antigravity",
            "sessionId": "agy-stub-2222",
            "title": "Continue task",
            "mtime": 1700000200.0,
            "initialUserGoal": "продолжи задачу за клодом",
            "modifiedFiles": [],
            "allTurns": [
                {"role": "user", "text": "продолжи задачу за клодом"},
            ],
        }
        all_parsed = [agy_meta_stub, claude_session]
        chosen = ab.select_best_failover_session(all_parsed, to_agent="current")
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["sessionId"], "claude-substantive-1111")

        chosen_agy = ab.select_best_failover_session(all_parsed, to_agent="agy")
        self.assertEqual(chosen_agy["sessionId"], "claude-substantive-1111")

        repo = self.tmp_dir / "failover-repo"
        self._init_git_repo(repo)
        # Also test stripping of <artifact-view-context> and <pasted_content> from Claude Desktop prompts
        claude_session["allTurns"][2]["text"] = (
            '<artifact-view-context artifact="d16a7846">\n{"context":{"mode":"edit"}}\n</artifact-view-context>\n'
            '<pasted_content id="6b0f">\nNow add remote events for buying seeds and restock timer\n</pasted_content id="6b0f">'
        )
        state_md = ab.build_auto_state_block("failover-repo", repo, "test-id", [claude_session], [])
        self.assertIn("Now add remote events for buying seeds and restock timer", state_md)
        self.assertIn("Implemented BuySeedRemote and 120s restock loop", state_md)
        self.assertNotIn("artifact-view-context", state_md)
        self.assertNotIn("pasted_content", state_md)
        self.assertNotIn("через mcp создай в антигравити", state_md)

    def test_10_strict_project_path_matching_no_substring_collision(self) -> None:
        path_strs = ["/Users/test/Documents/any", "/Users/test/.claude/worktrees/any-wt1"]
        self.assertTrue(ab.path_matches_project("/Users/test/Documents/any", path_strs, "any"))
        self.assertTrue(ab.path_matches_project("/Users/test/Documents/any/src/main.py", path_strs, "any"))
        self.assertTrue(ab.path_matches_project("/Users/test/.claude/worktrees/any-wt1", path_strs, "any"))
        self.assertFalse(ab.path_matches_project("/Users/test/Documents/company-bot", path_strs, "any"))
        self.assertFalse(ab.path_matches_project("/Users/test/Documents/many-scripts", path_strs, "any"))
        self.assertFalse(ab.path_matches_project("/Users/test/Documents/anybody", path_strs, "any"))

    def test_11_agy_conversation_filter_preserves_project_id_matches(self) -> None:
        base_repo = self.tmp_dir / "wt-main"
        wt_repo = self.tmp_dir / "wt-secondary"
        base_repo.mkdir(parents=True, exist_ok=True)
        wt_repo.mkdir(parents=True, exist_ok=True)

        orig_rpc = ab.agy_rpc
        try:
            ab.agy_rpc = lambda method, payload, timeout=10: (
                True,
                {
                    "trajectorySummaries": {
                        "cid-empty-uris": {
                            "summary": "Newly spawned cascade",
                            "stepCount": 2,
                            "status": "IDLE",
                            "lastModifiedTime": "2026-11-07T12:00:00Z",
                            "trajectoryMetadata": {"projectId": "proj-123", "workspaceUris": []},
                        },
                        "cid-worktree-uri": {
                            "summary": "Worktree cascade",
                            "stepCount": 15,
                            "status": "IDLE",
                            "lastModifiedTime": "2026-11-07T12:05:00Z",
                            "trajectoryMetadata": {
                                "projectId": "proj-123",
                                "workspaceUris": [ab.path_to_uri(wt_repo)],
                            },
                        },
                        "cid-other-project": {
                            "summary": "Unrelated project",
                            "stepCount": 9,
                            "status": "IDLE",
                            "lastModifiedTime": "2026-11-07T12:10:00Z",
                            "trajectoryMetadata": {
                                "projectId": "proj-999",
                                "workspaceUris": ["file:///Users/test/other"],
                            },
                        },
                    }
                },
            )
            convs = ab.list_agy_conversations(
                project_id="proj-123",
                workspace_path=base_repo,
                extra_workspace_paths=[base_repo, wt_repo],
            )
            cids = [c["cascadeId"] for c in convs]
            self.assertIn("cid-empty-uris", cids)
            self.assertIn("cid-worktree-uri", cids)
            self.assertNotIn("cid-other-project", cids)
        finally:
            ab.agy_rpc = orig_rpc

    def test_12_mcp_project_discovery_honors_env_and_recent_sessions(self) -> None:
        env_repo = self.tmp_dir / "env-override-repo"
        self._init_git_repo(env_repo)
        old_env = os.environ.get("CLAUDE_PROJECT_DIR")
        try:
            os.environ["CLAUDE_PROJECT_DIR"] = str(env_repo)
            found = ab.find_most_recent_active_project(prefer_recent_session=True)
            self.assertEqual(found, env_repo.resolve())
        finally:
            if old_env is None:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = old_env

    def test_13_active_dev_server_port_detection_and_open_questions(self) -> None:
        import socket

        repo = self.tmp_dir / "devsrv-repo"
        self._init_git_repo(repo)
        ab.record_repo_decision(
            repo,
            summary="Should SeedShop restock timer persist across server restarts?",
            reason="Need to decide between MemoryStoreService vs global epoch math",
            kind="open_question",
            agent="claude",
        )

        srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        old_cwd = os.getcwd()
        try:
            srv_sock.bind(("127.0.0.1", 0))
            srv_sock.listen(1)
            port = srv_sock.getsockname()[1]
            os.chdir(repo)

            found_servers = ab.discover_project_dev_servers([repo])
            ports = [s["port"] for s in found_servers]
            self.assertIn(port, ports)

            state_md = ab.build_auto_state_block("devsrv-repo", repo, "test-id", [], [])
            self.assertIn(f"localhost:{port}", state_md)
            self.assertIn("Active Dev Servers", state_md)
            self.assertIn("OPEN QUESTION", state_md)
            self.assertIn("Should SeedShop restock timer persist across server restarts?", state_md)

            handoff_md = ab.generate_handoff_markdown("devsrv-repo", repo, repo, "test-id", {})
            self.assertIn(f"localhost:{port}", handoff_md)
            self.assertIn("Active Dev Servers", handoff_md)
        finally:
            os.chdir(old_cwd)
            srv_sock.close()


    def test_14_docker_compose_ports_mapped_to_worktrees(self) -> None:
        from unittest import mock

        repo = self.tmp_dir / "compose-repo"
        self._init_git_repo(repo)
        wt = repo / ".claude" / "worktrees" / "feat-x"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "feat-x", str(wt)], check=True)
        (repo / "docker").mkdir()
        (wt / "docker").mkdir()
        all_paths = [repo, wt]

        # Ports column parsing: IPv4 + IPv6 dupes, ranges, unpublished and udp ports
        self.assertEqual(ab.parse_docker_host_ports("0.0.0.0:5432->5432/tcp, :::5432->5432/tcp"), [5432])
        self.assertEqual(ab.parse_docker_host_ports("[::]:8080->80/tcp, 6379/tcp, 0.0.0.0:53->53/udp"), [8080])
        self.assertEqual(ab.parse_docker_host_ports("127.0.0.1:9000-9002->9000-9002/tcp"), [9000, 9001, 9002])
        self.assertEqual(ab.parse_docker_host_ports(""), [])

        ps_out = "\n".join([
            # base repo stack, compose file in docker/ subfolder -> default project name `docker`
            "\t".join(["aaa111bbb222ccc", "docker-db-1", "0.0.0.0:5432->5432/tcp, :::5432->5432/tcp", "docker", str(repo / "docker"), "db"]),
            # worktree stack with unique project name
            "\t".join(["ddd333", "featx-web-1", "0.0.0.0:3001->3000/tcp", "compose-repo-feat-x", str(wt / "docker"), "web"]),
            # foreign project: must be ignored
            "\t".join(["eee444", "other-web-1", "0.0.0.0:3000->3000/tcp", "other", "/Users/someone/else", "web"]),
            # container with no published ports
            "\t".join(["fff555", "docker-redis-1", "6379/tcp", "docker", str(repo / "docker"), "redis"]),
            "garbage line",
        ])
        parsed = ab.parse_docker_compose_ps(ps_out, all_paths)
        by_port = {s["port"]: s for s in parsed}
        self.assertEqual(sorted(by_port), [3001, 5432])
        self.assertEqual(Path(by_port[5432]["worktree"]).resolve(), repo.resolve())
        # nested worktree must win over the base repo prefix (longest match)
        self.assertEqual(Path(by_port[3001]["worktree"]).resolve(), wt.resolve())
        self.assertTrue(by_port[5432]["compose_default_name_risk"])
        self.assertFalse(by_port[3001]["compose_default_name_risk"])
        self.assertEqual(by_port[5432]["kind"], "docker")
        self.assertEqual(by_port[5432]["compose_service"], "db")

        # discover_docker_compose_services: mocked docker CLI + short timeout honoured
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout=ps_out, stderr="")
        with mock.patch.object(ab, "_find_docker_bin", return_value="/usr/local/bin/docker"), \
             mock.patch.object(ab, "_docker_daemon_likely_up", return_value=True), \
             mock.patch.object(ab.subprocess, "run", return_value=fake) as m_run:
            got = ab.discover_docker_compose_services(all_paths)
        self.assertEqual(sorted(s["port"] for s in got), [3001, 5432])
        self.assertLessEqual(m_run.call_args.kwargs["timeout"], 0.8)
        self.assertIn("label=com.docker.compose.project", m_run.call_args.args[0])

        # Docker off / CLI timeout -> silently empty, never raises
        with mock.patch.object(ab, "_find_docker_bin", return_value="/usr/local/bin/docker"), \
             mock.patch.object(ab, "_docker_daemon_likely_up", return_value=True), \
             mock.patch.object(ab.subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 0.7)):
            self.assertEqual(ab.discover_docker_compose_services(all_paths), [])
        with mock.patch.object(ab, "_find_docker_bin", return_value=None):
            self.assertEqual(ab.discover_docker_compose_services(all_paths), [])

        # LIVE_STATE rendering from the feat-x worktree: own stack = REUSE, base repo stack = neighbor
        lines = "\n".join(ab.format_dev_servers_lines(parsed, wt))
        self.assertIn("THIS worktree** `feat-x`", lines)
        self.assertIn("localhost:3001", lines)
        self.assertIn("REUSE", lines)
        self.assertIn("neighbor worktree** `compose-repo`", lines)
        self.assertIn("do NOT bind 5432 here", lines)
        self.assertIn("COMPOSE_PROJECT_NAME", lines)
        self.assertIn("`docker`", lines)  # default-name clash warning

        # Full pipeline: docker results merged into build_auto_state_block
        with mock.patch.object(ab, "_discover_lsof_dev_servers", return_value=[]), \
             mock.patch.object(ab, "discover_docker_compose_services", return_value=parsed):
            merged = ab.discover_project_dev_servers(all_paths)
            self.assertEqual(sorted(s["port"] for s in merged), [3001, 5432])
            state_md = ab.build_auto_state_block("compose-repo", wt, "test-id", [], [])
        self.assertIn("docker:web", state_md)
        self.assertIn("do NOT bind 5432 here", state_md)


    def test_15_compose_preflight_and_foreign_port_holders(self) -> None:
        import socket
        from unittest import mock

        repo = self.tmp_dir / "preflight-repo"
        self._init_git_repo(repo)
        wt = repo / ".claude" / "worktrees" / "feat-y"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "feat-y", str(wt)], check=True)
        (wt / "docker").mkdir()
        (wt / "docker" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        all_paths = [repo, wt]

        # 1) parsing `docker compose config --format json`
        cfg = json.dumps({
            "name": "docker",
            "services": {
                "db": {"ports": [{"mode": "ingress", "host_ip": "127.0.0.1", "target": 5432, "published": "5432", "protocol": "tcp"}]},
                "web": {"ports": [{"target": 3000, "published": "3000-3001", "protocol": "tcp"},
                                  {"target": 9229, "protocol": "tcp"},          # ephemeral: ignored
                                  {"target": 53, "published": "5353", "protocol": "udp"}]},  # udp: ignored
                "worker": {},
            },
        })
        parsed = ab.parse_compose_config_ports(cfg)
        self.assertEqual(parsed["project"], "docker")
        self.assertEqual([(x["port"], x["service"]) for x in parsed["ports"]], [(3000, "web"), (3001, "web"), (5432, "db")])
        self.assertEqual(parsed["ports"][-1]["host_ip"], "127.0.0.1")
        self.assertEqual(ab.parse_compose_config_ports("not json")["ports"], [])

        # 2) .env: only *PORT* keys with numeric values, secrets never read into the result
        (wt / ".env").write_text("\n".join([
            "PORT=8080", "export VITE_PORT='5173'", "DB_PASSWORD=5432", "REPORT_LEVEL=3",
            "API_PORT=not-a-number", "SUPPORT_EMAIL=a@b.c", "# DB_PORT=1111",
        ]) + "\n", encoding="utf-8")
        self.assertEqual(ab.read_env_port_keys(wt / ".env"), {"PORT": 8080, "VITE_PORT": 5173})

        # compose units: subfolder detected; COMPOSE_FILE from root .env respected
        units = ab.find_compose_units(wt)
        self.assertEqual([u["dir"].name for u in units], ["docker"])
        (wt / "infra").mkdir()
        (wt / "infra" / "stack.yml").write_text("services: {}\n", encoding="utf-8")
        (wt / ".env").write_text("PORT=8080\nCOMPOSE_FILE=infra/stack.yml\n", encoding="utf-8")
        units = ab.find_compose_units(wt)
        self.assertEqual(units[0]["dir"], wt)
        self.assertEqual(units[0]["files"], [wt / "infra" / "stack.yml"])
        (wt / ".env").write_text("PORT=8080\n", encoding="utf-8")

        # 3) mtime cache: docker spawned once, again only after compose/.env change; cache-only when docker disallowed
        cache_dir = self.tmp_dir / "cache"
        cache_dir.mkdir()
        calls = []

        def fake_cfg(unit_dir):
            calls.append(unit_dir)
            return cfg

        with mock.patch.object(ab, "run_compose_config", side_effect=fake_cfg):
            r1 = ab.compose_preflight_ports(wt, cache_dir)
            r2 = ab.compose_preflight_ports(wt, cache_dir)
            self.assertEqual(len(calls), 1)
            self.assertEqual(sorted({x["port"] for x in r1}), [3000, 3001, 5432])
            self.assertEqual(r1, r2)
            time.sleep(0.01)
            (wt / "docker" / ".env").write_text("DB_PORT=5432\n", encoding="utf-8")
            ab.compose_preflight_ports(wt, cache_dir)
            self.assertEqual(len(calls), 2)
            (wt / "docker" / "docker-compose.yml").write_text("services: {x: {}}\n", encoding="utf-8")
            old = ab.PREFLIGHT_ALLOW_DOCKER
            ab.PREFLIGHT_ALLOW_DOCKER = False
            try:
                self.assertEqual(ab.compose_preflight_ports(wt, cache_dir), [])  # stale cache + no docker allowed
                self.assertEqual(len(calls), 2)
            finally:
                ab.PREFLIGHT_ALLOW_DOCKER = old

        # 4) timeout / missing docker / AGY_BRIDGE_NO_DOCKER=1 -> silently nothing
        with mock.patch.object(ab, "_find_docker_bin", return_value="/usr/local/bin/docker"), \
             mock.patch.object(ab.subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 1.5)) as m_run:
            self.assertIsNone(ab.run_compose_config(wt / "docker"))
            self.assertLessEqual(m_run.call_args.kwargs["timeout"], 1.5)
        with mock.patch.object(ab, "_find_docker_bin", return_value=None):
            self.assertIsNone(ab.run_compose_config(wt / "docker"))
        with mock.patch.dict(os.environ, {"AGY_BRIDGE_NO_DOCKER": "1"}), \
             mock.patch.object(ab.subprocess, "run") as m_never:
            self.assertIsNone(ab.run_compose_config(wt / "docker"))
            self.assertEqual(ab.compose_preflight_ports(wt, self.tmp_dir / "cache2"), [])
            m_never.assert_not_called()

        # 5) matching wanted ports against holders
        neighbor_db = {"port": 5432, "endpoint": "localhost:5432", "pid": 0, "process": "docker:db", "kind": "docker",
                       "compose_project": "docker", "worktree": str(repo)}
        own_web = {"port": 3000, "endpoint": "localhost:3000", "pid": 0, "process": "docker:web", "kind": "docker",
                   "compose_project": "docker", "worktree": str(wt)}
        compose_ports = [{**x, "project": "docker", "unit_dir": str(wt / "docker"), "source": f"compose:{x['service']}"}
                         for x in parsed["ports"]]
        foreign = {3001: [{"pid": 4242, "process": "postgres", "cwd": "/opt/homebrew/var", "args": "postgres -D /opt/homebrew/var"}],
                   8080: [{"pid": 777, "process": "nginx", "cwd": "/", "args": "nginx"}]}
        with mock.patch.object(ab, "compose_preflight_ports", return_value=compose_ports), \
             mock.patch.object(ab, "find_port_holders", side_effect=lambda pts: {p: foreign[p] for p in pts if p in foreign}) as m_h:
            conf = ab.preflight_port_conflicts(wt, all_paths, [neighbor_db, own_web], cache_dir=cache_dir)
        by = {c["port"]: c for c in conf}
        self.assertNotIn(3000, by)  # own running stack = reuse, not a conflict
        self.assertEqual(by[5432]["severity"], "block")
        self.assertIn("neighbor worktree `preflight-repo`", by[5432]["holder"])
        self.assertEqual(by[3001]["severity"], "block")
        self.assertIn("postgres", by[3001]["holder"])
        self.assertEqual(by[8080]["severity"], "warn")  # .env PORT held by an unrelated process
        self.assertIn("`.env PORT`", ab.format_preflight_lines(conf, wt)[-1])
        self.assertNotIn(5432, m_h.call_args.args[0])  # already explained by neighbor, no lsof needed

        lines = "\n".join(ab.format_preflight_lines(conf, wt))
        self.assertIn("⛔ Compose pre-flight** (`docker/`, project `docker`): wants host port `5432`", lines)
        self.assertIn("unrelated process `postgres` (PID 4242)", lines)
        self.assertIn("⚠️ Port `8080` needed by this project", lines)
        self.assertIn("nginx", lines)

        # 6) real lsof: an unrelated listener (cwd outside repo) on a .env port is reported, in-repo one is not
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            (wt / ".env").write_text(f"PORT={port}\n", encoding="utf-8")
            holders = ab.find_port_holders([port])
            self.assertIn(port, holders)
            self.assertEqual(holders[port][0]["pid"], os.getpid())
            old_cwd = os.getcwd()
            try:
                os.chdir(self.tmp_dir)  # this process is now "foreign" to the repo
                with mock.patch.object(ab, "compose_preflight_ports", return_value=[]):
                    conf = ab.preflight_port_conflicts(wt, all_paths, [], cache_dir=cache_dir)
                self.assertEqual([(c["port"], c["severity"], c["holder_kind"]) for c in conf], [(port, "warn", "foreign")])
                os.chdir(wt)  # now it belongs to this worktree -> nothing
                with mock.patch.object(ab, "compose_preflight_ports", return_value=[]):
                    self.assertEqual(ab.preflight_port_conflicts(wt, all_paths, [], cache_dir=cache_dir), [])
            finally:
                os.chdir(old_cwd)
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

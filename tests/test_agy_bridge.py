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


if __name__ == "__main__":
    unittest.main(verbosity=2)

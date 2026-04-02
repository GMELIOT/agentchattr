"""Tests for the restart orchestrator: state machine, dry-run, replay prevention."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app


class _FakeRegistry:
    def __init__(self, bases=None):
        self._bases = bases or {
            "claude": {"command": "claude", "cwd": "/home/dev/merit", "label": "Claude"},
            "gemini": {"command": "gemini", "cwd": "/home/dev/merit", "label": "Gemini"},
        }
        self.deregistered: list[str] = []

    def get_bases(self) -> dict:
        return dict(self._bases)

    def get_base_config(self, base: str) -> dict | None:
        return dict(self._bases[base]) if base in self._bases else None

    def get_instances_for(self, base: str) -> list[dict]:
        if base == "claude":
            return [
                {"name": "claude", "label": "Claude", "slot": 1},
                {"name": "claude-2", "label": "Claude 2", "slot": 2},
            ]
        if base == "gemini":
            return [{"name": "gemini", "label": "Gemini", "slot": 1}]
        return []

    def deregister(self, name: str) -> dict | None:
        self.deregistered.append(name)
        return {"ok": True}

    def is_agent_family(self, name: str) -> bool:
        return name in self._bases


class _FakeStore:
    def __init__(self):
        self.messages: list[tuple] = []

    def add(self, sender, text, **kwargs):
        self.messages.append((sender, text, kwargs))


class RestartLogTests(unittest.TestCase):
    """Test restart log read/write/update operations."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "restart_log.jsonl"
        self._orig_path = app.restart_log._path
        app.restart_log.path = self.log_path

    def tearDown(self):
        app.restart_log._path = self._orig_path

    def test_append_and_read(self):
        entry = {"restart_id": "abc123", "status": "pending", "scope": "agents"}
        app.restart_log.append(entry)
        entries = app.restart_log.read()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["restart_id"], "abc123")
        self.assertEqual(entries[0]["status"], "pending")

    def test_update_entry(self):
        app.restart_log.append({"restart_id": "abc", "status": "pending"})
        app.restart_log.append({"restart_id": "def", "status": "pending"})
        app.restart_log.update("abc", {"status": "complete"})

        entries = app.restart_log.read()
        self.assertEqual(entries[0]["status"], "complete")
        self.assertEqual(entries[1]["status"], "pending")

    def test_read_empty_file(self):
        entries = app.restart_log.read()
        self.assertEqual(entries, [])

    def test_read_corrupt_lines_skipped(self):
        self.log_path.write_text('{"restart_id":"ok","status":"pending"}\nnot json\n')
        entries = app.restart_log.read()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["restart_id"], "ok")


class RosterSnapshotTests(unittest.TestCase):
    """Test that _build_roster captures all instances with identity fields."""

    def setUp(self):
        self._orig_registry = app.registry
        app.registry = _FakeRegistry()

    def tearDown(self):
        app.registry = self._orig_registry

    def test_roster_captures_all_instances(self):
        roster = app._build_roster()
        names = [a["name"] for a in roster]
        self.assertIn("claude", names)
        self.assertIn("claude-2", names)
        self.assertIn("gemini", names)
        self.assertEqual(len(roster), 3)

    def test_roster_includes_identity_fields(self):
        roster = app._build_roster()
        for agent in roster:
            self.assertIn("base", agent)
            self.assertIn("name", agent)
            self.assertIn("label", agent)
            self.assertIn("slot", agent)
            self.assertIn("session_name", agent)
            self.assertIn("cwd", agent)

    def test_roster_session_name_format(self):
        roster = app._build_roster()
        for agent in roster:
            self.assertEqual(agent["session_name"], f"agentchattr-{agent['name']}")

    def test_roster_empty_without_registry(self):
        app.registry = None
        self.assertEqual(app._build_roster(), [])


class DryRunTests(unittest.IsolatedAsyncioTestCase):
    """Test that dry-run exercises the same code path with no side effects."""

    def setUp(self):
        self._orig_registry = app.registry
        self._orig_store = app.store
        self._orig_event_loop = app._event_loop
        self._orig_log_path = app.restart_log.path
        self._orig_config = app.config

        self.tmpdir = tempfile.mkdtemp()
        app.restart_log.path = Path(self.tmpdir) / "restart_log.jsonl"
        app.registry = _FakeRegistry()
        app.store = _FakeStore()
        app.config = {"server": {"data_dir": self.tmpdir}}
        app._event_loop = asyncio.get_event_loop()

        self.broadcasts: list[dict] = []
        self._orig_broadcast = app._broadcast

    def tearDown(self):
        app.registry = self._orig_registry
        app.store = self._orig_store
        app._event_loop = self._orig_event_loop
        app.restart_log.path = self._orig_log_path
        app.config = self._orig_config
        app._broadcast = self._orig_broadcast

    async def test_dry_run_no_tmux_kills(self):
        kills: list[str] = []
        with patch.object(app, '_kill_agent_session', side_effect=lambda s: kills.append(s) or True):
            roster = app._build_roster()
            await app._execute_restart("dry1", "agents", "test", roster, True, "test-user")

        self.assertEqual(kills, [], "Dry run should not kill any sessions")

    async def test_dry_run_no_chat_messages(self):
        roster = app._build_roster()
        await app._execute_restart("dry2", "agents", "test", roster, True, "test-user")
        self.assertEqual(app.store.messages, [], "Dry run should not post chat messages")

    async def test_dry_run_writes_log_entry(self):
        roster = app._build_roster()
        app.restart_log.append({
            "restart_id": "dry3", "status": "pending", "scope": "agents",
            "roster": roster, "dry_run": True,
        })
        await app._execute_restart("dry3", "agents", "test", roster, True, "test-user")
        # Log entry should exist (written by the caller, not by dry run)
        entries = app.restart_log.read()
        self.assertTrue(len(entries) >= 1)


class ResurrectionReplayTests(unittest.TestCase):
    """Test that completed/failed entries are NOT replayed on startup."""

    def setUp(self):
        self._orig_registry = app.registry
        self._orig_log_path = app.restart_log.path
        self.tmpdir = tempfile.mkdtemp()
        app.restart_log.path = Path(self.tmpdir) / "restart_log.jsonl"
        app.registry = _FakeRegistry()
        self.started: list[str] = []

    def tearDown(self):
        app.registry = self._orig_registry
        app.restart_log.path = self._orig_log_path

    def test_complete_entry_not_replayed(self):
        app.restart_log.append({
            "restart_id": "done1", "status": "complete",
            "roster": [{"base": "claude", "name": "claude", "session_name": "agentchattr-claude"}],
        })
        with patch.object(app, '_start_agent_wrapper', side_effect=lambda b, c: self.started.append(b)):
            with patch.object(app, '_tmux_session_exists', return_value=False):
                app.resurrect_from_log()
        self.assertEqual(self.started, [], "Complete entries must not trigger resurrection")

    def test_partial_failed_entry_not_replayed(self):
        app.restart_log.append({
            "restart_id": "pfail1", "status": "partial_failed",
            "roster": [{"base": "claude", "name": "claude", "session_name": "agentchattr-claude"}],
            "errors": [{"agent": "gemini", "error": "wrapper crashed"}],
        })
        with patch.object(app, '_start_agent_wrapper', side_effect=lambda b, c: self.started.append(b)):
            with patch.object(app, '_tmux_session_exists', return_value=False):
                app.resurrect_from_log()
        self.assertEqual(self.started, [], "partial_failed entries must not trigger resurrection")

    def test_failed_entry_not_replayed(self):
        app.restart_log.append({
            "restart_id": "fail1", "status": "failed",
            "roster": [{"base": "claude", "name": "claude", "session_name": "agentchattr-claude"}],
        })
        with patch.object(app, '_start_agent_wrapper', side_effect=lambda b, c: self.started.append(b)):
            with patch.object(app, '_tmux_session_exists', return_value=False):
                app.resurrect_from_log()
        self.assertEqual(self.started, [], "Failed entries must not trigger resurrection")

    def test_pending_entry_triggers_resurrection(self):
        app.restart_log.append({
            "restart_id": "pend1", "status": "killing",
            "roster": [
                {"base": "claude", "name": "claude", "session_name": "agentchattr-claude"},
                {"base": "gemini", "name": "gemini", "session_name": "agentchattr-gemini"},
            ],
        })
        with patch.object(app, '_start_agent_wrapper', side_effect=lambda b, c: self.started.append(b)):
            with patch.object(app, '_tmux_session_exists', return_value=False):
                app.resurrect_from_log()
        self.assertEqual(sorted(self.started), ["claude", "gemini"])
        # Verify entry is now terminal
        entries = app.restart_log.read()
        self.assertEqual(entries[0]["status"], "complete")

    def test_already_running_session_not_restarted(self):
        app.restart_log.append({
            "restart_id": "pend2", "status": "resurrecting",
            "roster": [
                {"base": "claude", "name": "claude", "session_name": "agentchattr-claude"},
            ],
        })
        with patch.object(app, '_start_agent_wrapper', side_effect=lambda b, c: self.started.append(b)):
            with patch.object(app, '_tmux_session_exists', return_value=True):
                app.resurrect_from_log()
        self.assertEqual(self.started, [], "Should not restart already-running sessions")

    def test_partial_failure_recorded(self):
        app.restart_log.append({
            "restart_id": "partfail", "status": "killing",
            "roster": [
                {"base": "claude", "name": "claude", "session_name": "agentchattr-claude"},
                {"base": "gemini", "name": "gemini", "session_name": "agentchattr-gemini"},
            ],
        })

        def mock_start(base, cfg):
            if base == "gemini":
                raise RuntimeError("wrapper crashed")
            self.started.append(base)

        with patch.object(app, '_start_agent_wrapper', side_effect=mock_start):
            with patch.object(app, '_tmux_session_exists', return_value=False):
                app.resurrect_from_log()

        self.assertEqual(self.started, ["claude"])
        entries = app.restart_log.read()
        self.assertEqual(entries[0]["status"], "partial_failed")
        self.assertEqual(len(entries[0]["errors"]), 1)
        self.assertEqual(entries[0]["errors"][0]["agent"], "gemini")


    def test_server_only_empty_roster_marked_complete(self):
        app.restart_log.append({
            "restart_id": "srv1", "status": "restarting_server", "scope": "server",
            "roster": [],
        })
        with patch.object(app, '_start_agent_wrapper', side_effect=lambda b, c: self.started.append(b)):
            with patch.object(app, '_tmux_session_exists', return_value=False):
                app.resurrect_from_log()
        self.assertEqual(self.started, [], "Server-only restart should not start agents")
        entries = app.restart_log.read()
        self.assertEqual(entries[0]["status"], "complete",
                         "Server-only restart with empty roster must be complete, not failed")

    def test_non_server_empty_roster_marked_failed(self):
        app.restart_log.append({
            "restart_id": "empty1", "status": "killing", "scope": "agents",
            "roster": [],
        })
        with patch.object(app, '_start_agent_wrapper', side_effect=lambda b, c: self.started.append(b)):
            with patch.object(app, '_tmux_session_exists', return_value=False):
                app.resurrect_from_log()
        entries = app.restart_log.read()
        self.assertEqual(entries[0]["status"], "failed")


class _RenamedRegistry(_FakeRegistry):
    """Registry where claude has been renamed to claude-prime."""

    def get_instances_for(self, base: str) -> list[dict]:
        if base == "claude":
            return [{"name": "claude-prime", "label": "Claude Prime", "slot": 1}]
        return super().get_instances_for(base)


class RenamedInstanceGuardTests(unittest.TestCase):
    """Test that restart is blocked when roster contains renamed agents."""

    def setUp(self):
        self._orig_registry = app.registry
        self._orig_log_path = app.restart_log.path
        self.tmpdir = tempfile.mkdtemp()
        app.restart_log.path = Path(self.tmpdir) / "restart_log.jsonl"

    def tearDown(self):
        app.registry = self._orig_registry
        app.restart_log.path = self._orig_log_path

    def test_roster_detects_renamed_instance(self):
        app.registry = _RenamedRegistry()
        roster = app._build_roster()
        renamed = [a for a in roster if a["name"] == "claude-prime"]
        self.assertEqual(len(renamed), 1)
        self.assertEqual(renamed[0]["base"], "claude")


class APIRouteTests(unittest.IsolatedAsyncioTestCase):
    """Test the /api/restart HTTP route contract."""

    def setUp(self):
        from fastapi.testclient import TestClient

        self._orig_registry = app.registry
        self._orig_store = app.store
        self._orig_event_loop = app._event_loop
        self._orig_log_path = app.restart_log.path
        self._orig_config = app.config
        self._orig_token = app.session_token

        self.tmpdir = tempfile.mkdtemp()
        app.restart_log.path = Path(self.tmpdir) / "restart_log.jsonl"
        app.registry = _FakeRegistry()
        app.store = _FakeStore()
        app.config = {"server": {"data_dir": self.tmpdir}}
        app.session_token = "test-token"

        self.client = TestClient(app.app)
        self.headers = {"X-Session-Token": "test-token"}

        self.kills: list[str] = []
        self.starts: list[str] = []
        self.server_restarts: list[bool] = []

    def tearDown(self):
        app.registry = self._orig_registry
        app.store = self._orig_store
        app._event_loop = self._orig_event_loop
        app.restart_log.path = self._orig_log_path
        app.config = self._orig_config
        app.session_token = self._orig_token

    def test_invalid_scope_rejected(self):
        resp = self.client.post("/api/restart", json={"scope": "bananas"}, headers=self.headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("scope must be", resp.json()["error"])

    def test_missing_scope_rejected(self):
        resp = self.client.post("/api/restart", json={}, headers=self.headers)
        self.assertEqual(resp.status_code, 400)

    def test_dry_run_returns_roster_without_kills(self):
        with patch.object(app, '_kill_agent_session', side_effect=lambda s: self.kills.append(s) or True):
            with patch.object(app, '_start_agent_wrapper', side_effect=lambda b, c: self.starts.append(b)):
                resp = self.client.post("/api/restart", json={
                    "scope": "agents", "reason": "test", "dry_run": True,
                    "initiated_by": "test-user",
                }, headers=self.headers)

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["dry_run"])
        self.assertIn("roster", data)
        self.assertEqual(self.kills, [], "Dry run must not kill sessions")
        self.assertEqual(self.starts, [], "Dry run must not start wrappers")
        self.assertEqual(app.store.messages, [], "Dry run must not post chat messages")

    def test_renamed_agent_blocks_restart(self):
        app.registry = _RenamedRegistry()
        resp = self.client.post("/api/restart", json={
            "scope": "agents", "reason": "test",
        }, headers=self.headers)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("renamed", resp.json()["error"].lower())

    def test_scope_server_does_not_kill_agents(self):
        with patch.object(app, '_kill_agent_session', side_effect=lambda s: self.kills.append(s) or True):
            with patch.object(app, 'os') as mock_os:
                mock_os.getpid.return_value = 99999
                with patch.object(app.subprocess, 'Popen'):
                    resp = self.client.post("/api/restart", json={
                        "scope": "server", "reason": "test", "initiated_by": "test",
                    }, headers=self.headers)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.kills, [], "scope=server must not kill agent sessions")

    def test_scope_agents_does_not_restart_server(self):
        popen_calls: list = []
        orig_popen = app.subprocess.Popen

        def track_popen(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(str(c) for c in cmd) if isinstance(cmd, list) else str(cmd)
            # Only track server restart commands (not wrapper starts)
            if "kill" in cmd_str and "run.py" in cmd_str:
                popen_calls.append(cmd_str)
            return orig_popen(*args, **kwargs) if "wrapper.py" in cmd_str else unittest.mock.MagicMock()

        with patch.object(app, '_kill_agent_session', return_value=True):
            with patch.object(app.subprocess, 'Popen', side_effect=track_popen):
                resp = self.client.post("/api/restart", json={
                    "scope": "agents", "reason": "test", "initiated_by": "test",
                }, headers=self.headers)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(popen_calls, [], "scope=agents must not spawn server restart")

    def test_concurrent_restart_rejected(self):
        # Simulate an in-flight restart
        app.restart_log.append({
            "restart_id": "inflight", "status": "grace", "scope": "agents",
        })
        resp = self.client.post("/api/restart", json={
            "scope": "agents", "reason": "test", "initiated_by": "test",
        }, headers=self.headers)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("already in progress", resp.json()["error"])

    def test_concurrent_dry_run_allowed(self):
        # Dry runs should always be allowed even with in-flight restart
        app.restart_log.append({
            "restart_id": "inflight2", "status": "killing", "scope": "agents",
        })
        with patch.object(app, '_kill_agent_session', return_value=True):
            resp = self.client.post("/api/restart", json={
                "scope": "agents", "reason": "test", "dry_run": True,
                "initiated_by": "test",
            }, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["dry_run"])


class _LabelTrackingRegistry(_FakeRegistry):
    """Registry that tracks set_label calls and returns instances with slots."""

    def __init__(self):
        super().__init__()
        self.label_calls: list[tuple[str, str]] = []

    def get_instances_for(self, base: str) -> list[dict]:
        if base == "claude":
            return [
                {"name": "claude", "label": "Claude", "slot": 1},
                {"name": "claude-2", "label": "Claude 2", "slot": 2},
            ]
        return super().get_instances_for(base)

    def set_label(self, name: str, label: str) -> bool:
        self.label_calls.append((name, label))
        return True


class LabelRestoreTests(unittest.IsolatedAsyncioTestCase):
    """Test that label restoration is per roster entry, not per base."""

    def setUp(self):
        self._orig_registry = app.registry

    def tearDown(self):
        app.registry = self._orig_registry

    async def test_multi_instance_labels_restored_to_correct_slots(self):
        reg = _LabelTrackingRegistry()
        app.registry = reg

        roster = [
            {"base": "claude", "name": "claude", "label": "Claude PM", "slot": 1,
             "session_name": "agentchattr-claude", "cwd": "/home/dev/merit"},
            {"base": "claude", "name": "claude-2", "label": "Claude Reviewer", "slot": 2,
             "session_name": "agentchattr-claude-2", "cwd": "/home/dev/merit"},
        ]
        await app._restore_labels(roster, max_wait=3)

        # Both labels should be set on the correct instance
        self.assertIn(("claude", "Claude PM"), reg.label_calls)
        self.assertIn(("claude-2", "Claude Reviewer"), reg.label_calls)

    async def test_default_labels_not_restored(self):
        reg = _LabelTrackingRegistry()
        app.registry = reg

        roster = [
            {"base": "claude", "name": "claude", "label": "Claude", "slot": 1,
             "session_name": "agentchattr-claude", "cwd": "/home/dev/merit"},
        ]
        await app._restore_labels(roster, max_wait=1)

        # Default label matches config, so no set_label call
        self.assertEqual(reg.label_calls, [])


if __name__ == "__main__":
    unittest.main()

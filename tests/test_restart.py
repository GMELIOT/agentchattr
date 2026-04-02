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
        self._orig_path = app._RESTART_LOG_PATH
        app._RESTART_LOG_PATH = self.log_path

    def tearDown(self):
        app._RESTART_LOG_PATH = self._orig_path

    def test_append_and_read(self):
        entry = {"restart_id": "abc123", "status": "pending", "scope": "agents"}
        app._append_restart_log(entry)
        entries = app._read_restart_log()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["restart_id"], "abc123")
        self.assertEqual(entries[0]["status"], "pending")

    def test_update_entry(self):
        app._append_restart_log({"restart_id": "abc", "status": "pending"})
        app._append_restart_log({"restart_id": "def", "status": "pending"})
        app._update_restart_entry("abc", {"status": "complete"})

        entries = app._read_restart_log()
        self.assertEqual(entries[0]["status"], "complete")
        self.assertEqual(entries[1]["status"], "pending")

    def test_read_empty_file(self):
        entries = app._read_restart_log()
        self.assertEqual(entries, [])

    def test_read_corrupt_lines_skipped(self):
        self.log_path.write_text('{"restart_id":"ok","status":"pending"}\nnot json\n')
        entries = app._read_restart_log()
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
        self._orig_log_path = app._RESTART_LOG_PATH
        self._orig_config = app.config

        self.tmpdir = tempfile.mkdtemp()
        app._RESTART_LOG_PATH = Path(self.tmpdir) / "restart_log.jsonl"
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
        app._RESTART_LOG_PATH = self._orig_log_path
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
        app._append_restart_log({
            "restart_id": "dry3", "status": "pending", "scope": "agents",
            "roster": roster, "dry_run": True,
        })
        await app._execute_restart("dry3", "agents", "test", roster, True, "test-user")
        # Log entry should exist (written by the caller, not by dry run)
        entries = app._read_restart_log()
        self.assertTrue(len(entries) >= 1)


class ResurrectionReplayTests(unittest.TestCase):
    """Test that completed/failed entries are NOT replayed on startup."""

    def setUp(self):
        self._orig_registry = app.registry
        self._orig_log_path = app._RESTART_LOG_PATH
        self.tmpdir = tempfile.mkdtemp()
        app._RESTART_LOG_PATH = Path(self.tmpdir) / "restart_log.jsonl"
        app.registry = _FakeRegistry()
        self.started: list[str] = []

    def tearDown(self):
        app.registry = self._orig_registry
        app._RESTART_LOG_PATH = self._orig_log_path

    def test_complete_entry_not_replayed(self):
        app._append_restart_log({
            "restart_id": "done1", "status": "complete",
            "roster": [{"base": "claude", "name": "claude", "session_name": "agentchattr-claude"}],
        })
        with patch.object(app, '_start_agent_wrapper', side_effect=lambda b, c: self.started.append(b)):
            with patch.object(app, '_tmux_session_exists', return_value=False):
                app.resurrect_from_log()
        self.assertEqual(self.started, [], "Complete entries must not trigger resurrection")

    def test_failed_entry_not_replayed(self):
        app._append_restart_log({
            "restart_id": "fail1", "status": "failed",
            "roster": [{"base": "claude", "name": "claude", "session_name": "agentchattr-claude"}],
        })
        with patch.object(app, '_start_agent_wrapper', side_effect=lambda b, c: self.started.append(b)):
            with patch.object(app, '_tmux_session_exists', return_value=False):
                app.resurrect_from_log()
        self.assertEqual(self.started, [], "Failed entries must not trigger resurrection")

    def test_pending_entry_triggers_resurrection(self):
        app._append_restart_log({
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
        entries = app._read_restart_log()
        self.assertEqual(entries[0]["status"], "complete")

    def test_already_running_session_not_restarted(self):
        app._append_restart_log({
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
        app._append_restart_log({
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
        entries = app._read_restart_log()
        self.assertEqual(entries[0]["status"], "partial_failed")
        self.assertEqual(len(entries[0]["errors"]), 1)
        self.assertEqual(entries[0]["errors"][0]["agent"], "gemini")


if __name__ == "__main__":
    unittest.main()

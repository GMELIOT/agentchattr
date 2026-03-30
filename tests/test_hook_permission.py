import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
from permission_policy import PermissionPolicy
from permission_store import PermissionStore


class FakeRequest:
    def __init__(
        self,
        body: dict,
        *,
        headers: dict | None = None,
        query_params: dict | None = None,
        client_host: str = "127.0.0.1",
    ):
        self._body = body
        self.headers = headers or {}
        self.query_params = query_params or {}
        self.client = type("Client", (), {"host": client_host})()

    async def json(self):
        return self._body


class PermissionHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "permissions.db"
        app.permission_store = PermissionStore(db_path=db_path)
        app.permission_policy = None
        app.permission_hook_secret = "hook-secret"
        app.session_token = "session-token"

    def tearDown(self):
        app.permission_store = None
        self._tmpdir.cleanup()

    def _request(self, body: dict, *, secret: str | None = "hook-secret") -> FakeRequest:
        headers = {}
        if secret is not None:
            headers["x-hook-secret"] = secret
        return FakeRequest(body, headers=headers)

    async def test_missing_hook_secret_is_rejected(self):
        response = await app.permission_request_hook(
            self._request(
                {
                    "session_id": "sess-auth",
                    "hook_event_name": "PermissionRequest",
                    "tool_name": "Bash",
                    "tool_input": {"command": "git status"},
                },
                secret=None,
            )
        )

        self.assertEqual(response.status_code, 403)
        payload = json.loads(response.body)
        self.assertEqual(
            payload["error"],
            "forbidden: invalid or missing permission hook secret",
        )
        self.assertEqual(app.permission_store.get_pending(), [])

    async def test_auto_allow_returns_immediately_without_creating_pending(self):
        app.permission_policy = PermissionPolicy(
            auto_allow=[r"Bash", r"Inspect .+", r"git status"],
            always_ask=[],
            dry_run=False,
        )

        response = await app.permission_request_hook(
            self._request(
                {
                    "session_id": "sess-1",
                    "hook_event_name": "PermissionRequest",
                    "tool_name": "Bash",
                    "tool_input": {
                        "description": "Inspect system state",
                        "command": "git status",
                    },
                }
            )
        )

        payload = json.loads(response.body)
        self.assertEqual(payload["hookSpecificOutput"]["decision"]["behavior"], "allow")
        self.assertEqual(app.permission_store.get_pending(), [])

    async def test_user_response_unblocks_hook_and_returns_allow(self):
        task = asyncio.create_task(
            app.permission_request_hook(
                self._request(
                    {
                        "session_id": "sess-3",
                        "hook_event_name": "PermissionRequest",
                        "tool_name": "Bash",
                        "tool_input": {
                            "description": "Remove node_modules directory",
                            "command": "rm -rf node_modules",
                        },
                    }
                )
            )
        )

        await asyncio.sleep(0.1)
        pending = app.permission_store.get_pending()
        self.assertEqual(len(pending), 1)
        perm = pending[0]
        self.assertEqual(perm["status"], "pending")
        self.assertEqual(perm["description"], "rm -rf node_modules")
        self.assertEqual(
            perm["input_preview"],
            '{"command": "rm -rf node_modules", "description": "Remove node_modules directory"}',
        )

        await app.respond_permission(
            perm["id"],
            FakeRequest({"key": "allow", "action": "approve"}),
        )
        response = await task

        payload = json.loads(response.body)
        self.assertEqual(payload["hookSpecificOutput"]["decision"]["behavior"], "allow")
        resolved = app.permission_store.get(perm["id"])
        self.assertEqual(resolved["status"], "approved")
        self.assertEqual(resolved["resolved_via"], "ui")

    async def test_dangerous_command_creates_pending_permission(self):
        app.permission_policy = PermissionPolicy(
            auto_allow=[r"Inspect .+"],
            always_ask=[r"rm -rf .+"],
            dry_run=False,
        )

        # Start the hook (it will block waiting for resolution)
        task = asyncio.create_task(
            app.permission_request_hook(
                self._request(
                    {
                        "session_id": "sess-4",
                        "hook_event_name": "PermissionRequest",
                        "tool_name": "Bash",
                        "tool_input": {
                            "description": "Inspect repo status",
                            "command": "rm -rf node_modules",
                        },
                    }
                )
            )
        )

        await asyncio.sleep(0.1)
        pending = app.permission_store.get_pending()
        self.assertEqual(len(pending), 1)
        perm = pending[0]
        self.assertEqual(perm["description"], "rm -rf node_modules")

        # Cancel to unblock the task
        app.permission_store.cancel_all_pending()
        response = await task
        payload = json.loads(response.body)
        self.assertEqual(payload["hookSpecificOutput"]["decision"]["behavior"], "deny")

    async def test_duplicate_resolve_returns_conflict(self):
        """First-writer-wins: second resolve attempt returns 409."""
        task = asyncio.create_task(
            app.permission_request_hook(
                self._request(
                    {
                        "session_id": "sess-5",
                        "hook_event_name": "PermissionRequest",
                        "tool_name": "Bash",
                        "tool_input": {"command": "echo hello"},
                    }
                )
            )
        )

        await asyncio.sleep(0.1)
        pending = app.permission_store.get_pending()
        self.assertEqual(len(pending), 1)
        perm_id = pending[0]["id"]

        # First resolve succeeds
        await app.respond_permission(
            perm_id,
            FakeRequest({"key": "allow", "action": "approve"}),
        )
        response = await task

        # Second resolve returns 409
        result = await app.respond_permission(
            perm_id,
            FakeRequest({"key": "deny", "action": "deny"}),
        )
        self.assertEqual(result.status_code, 409)

    async def test_persistence_survives_store_recreation(self):
        """Permissions persist in SQLite and survive store recreation (simulating restart)."""
        task = asyncio.create_task(
            app.permission_request_hook(
                self._request(
                    {
                        "session_id": "sess-6",
                        "hook_event_name": "PermissionRequest",
                        "tool_name": "Bash",
                        "tool_input": {"command": "ls"},
                    }
                )
            )
        )

        await asyncio.sleep(0.1)
        pending = app.permission_store.get_pending()
        self.assertEqual(len(pending), 1)
        perm_id = pending[0]["id"]

        # Recreate the store (simulates server restart)
        db_path = app.permission_store._db_path
        app.permission_store = PermissionStore(db_path=db_path)

        # Permission should still be there
        rehydrated = app.permission_store.get_pending()
        self.assertEqual(len(rehydrated), 1)
        self.assertEqual(rehydrated[0]["id"], perm_id)
        self.assertEqual(rehydrated[0]["status"], "pending")

        # Cancel to clean up the blocking task
        app.permission_store.cancel_all_pending()
        await task


    async def test_concurrent_ui_and_telegram_resolution_first_writer_wins(self):
        """Concurrent approvals from UI and Telegram: first writer wins, second is harmless."""
        # Create a pending permission via the hook (non-blocking — we'll resolve it)
        task = asyncio.create_task(
            app.permission_request_hook(
                self._request(
                    {
                        "session_id": "sess-concurrent",
                        "hook_event_name": "PermissionRequest",
                        "tool_name": "Bash",
                        "tool_input": {"command": "echo concurrent"},
                    }
                )
            )
        )

        await asyncio.sleep(0.1)
        pending = app.permission_store.get_pending()
        self.assertEqual(len(pending), 1)
        perm_id = pending[0]["id"]

        # Simulate concurrent resolution: UI approves, Telegram denies at ~same time
        # Since _resolve_permission uses SQLite with locking, one will win
        ui_result = asyncio.create_task(
            app.respond_permission(
                perm_id,
                FakeRequest({"key": "allow", "action": "approve"}),
            )
        )
        telegram_result = asyncio.create_task(
            app.respond_permission(
                perm_id,
                FakeRequest({"key": "deny", "action": "deny"}),
            )
        )

        results = await asyncio.gather(ui_result, telegram_result)

        # Exactly one should succeed (200), the other should get 409
        status_codes = []
        for r in results:
            if hasattr(r, 'status_code'):
                status_codes.append(r.status_code)
            elif isinstance(r, dict) and "ok" in r:
                status_codes.append(200)
            else:
                status_codes.append(getattr(r, 'status_code', None))

        self.assertIn(200, status_codes)
        self.assertIn(409, status_codes)

        # The permission should be in a terminal state
        perm = app.permission_store.get(perm_id)
        self.assertIn(perm["status"], ("approved", "denied"))

        # resolved_via should be set
        self.assertIn(perm["resolved_via"], ("ui",))  # both go through respond_permission → "ui"

        # Clean up the hook task
        response = await task
        payload = json.loads(response.body)
        # The hook should have gotten a response (allow or deny depending on who won)
        self.assertIn(
            payload["hookSpecificOutput"]["decision"]["behavior"],
            ("allow", "deny"),
        )

    async def test_cancel_all_pending_resolves_blocked_hook(self):
        """cancel_all_pending transitions permissions to cancelled, unblocking waiting hooks."""
        task = asyncio.create_task(
            app.permission_request_hook(
                self._request(
                    {
                        "session_id": "sess-cancel",
                        "hook_event_name": "PermissionRequest",
                        "tool_name": "Bash",
                        "tool_input": {"command": "echo cancel-test"},
                    }
                )
            )
        )

        await asyncio.sleep(0.1)
        pending = app.permission_store.get_pending()
        self.assertEqual(len(pending), 1)

        # Cancel all pending
        cancelled = app.permission_store.cancel_all_pending()
        self.assertEqual(len(cancelled), 1)

        response = await task
        payload = json.loads(response.body)
        self.assertEqual(payload["hookSpecificOutput"]["decision"]["behavior"], "deny")

        # Verify the permission is cancelled in the store with full audit
        perm = app.permission_store.get(pending[0]["id"])
        self.assertEqual(perm["status"], "cancelled")
        self.assertEqual(perm["resolved_by"], "system")
        self.assertEqual(perm["resolved_via"], "system")
        self.assertIsNotNone(perm["resolved_at"])


    async def test_duplicate_request_id_returns_existing_pending(self):
        """Retry/reconnect with same request_id returns existing pending permission,
        not a duplicate row."""
        # First hook call creates a pending permission
        task1 = asyncio.create_task(
            app.permission_request_hook(
                self._request(
                    {
                        "session_id": "sess-dedup",
                        "hook_event_name": "PermissionRequest",
                        "tool_name": "Bash",
                        "tool_input": {"command": "echo dedup"},
                    }
                )
            )
        )
        await asyncio.sleep(0.1)

        pending = app.permission_store.get_pending()
        self.assertEqual(len(pending), 1)
        first_id = pending[0]["id"]

        # Second hook call with the same session_id (simulating retry)
        task2 = asyncio.create_task(
            app.permission_request_hook(
                self._request(
                    {
                        "session_id": "sess-dedup",
                        "hook_event_name": "PermissionRequest",
                        "tool_name": "Bash",
                        "tool_input": {"command": "echo dedup"},
                    }
                )
            )
        )
        await asyncio.sleep(0.1)

        # Should still be exactly 1 pending permission
        pending = app.permission_store.get_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], first_id)

        # Resolve once — should unblock both hooks
        await app.respond_permission(
            first_id,
            FakeRequest({"key": "allow", "action": "approve"}),
        )

        response1 = await task1
        response2 = await task2
        payload1 = json.loads(response1.body)
        payload2 = json.loads(response2.body)
        self.assertEqual(payload1["hookSpecificOutput"]["decision"]["behavior"], "allow")
        self.assertEqual(payload2["hookSpecificOutput"]["decision"]["behavior"], "allow")

    async def test_cancel_all_pending_sets_full_audit_trail(self):
        """cancel_all_pending routes through transition() so resolved_via and
        resolved_at are set (not just resolved_by)."""
        # Create two pending permissions
        task1 = asyncio.create_task(
            app.permission_request_hook(
                self._request(
                    {
                        "session_id": "sess-audit-1",
                        "hook_event_name": "PermissionRequest",
                        "tool_name": "Bash",
                        "tool_input": {"command": "echo audit1"},
                    }
                )
            )
        )
        task2 = asyncio.create_task(
            app.permission_request_hook(
                self._request(
                    {
                        "session_id": "sess-audit-2",
                        "hook_event_name": "PermissionRequest",
                        "tool_name": "Bash",
                        "tool_input": {"command": "echo audit2"},
                    }
                )
            )
        )
        await asyncio.sleep(0.1)
        pending = app.permission_store.get_pending()
        self.assertEqual(len(pending), 2)

        cancelled = app.permission_store.cancel_all_pending()
        self.assertEqual(len(cancelled), 2)

        # Both should have full audit fields
        for perm in cancelled:
            self.assertEqual(perm["status"], "cancelled")
            self.assertEqual(perm["resolved_by"], "system")
            self.assertEqual(perm["resolved_via"], "system")
            self.assertIsNotNone(perm["resolved_at"])

        # Clean up tasks
        await task1
        await task2


if __name__ == "__main__":
    unittest.main()

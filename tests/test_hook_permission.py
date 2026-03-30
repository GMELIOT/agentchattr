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


if __name__ == "__main__":
    unittest.main()

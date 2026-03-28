import asyncio
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
from permission_policy import PermissionPolicy


class FakeRequest:
    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body


class PermissionHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app.pending_permissions.clear()
        app.permission_policy = None
        app.permission_auto_expire_seconds = 300

    async def test_auto_allow_returns_immediately_without_creating_permission(self):
        app.permission_policy = PermissionPolicy(
            auto_allow=[r"Inspect .+"],
            always_ask=[],
            dry_run=False,
        )

        response = await app.permission_request_hook(
            FakeRequest(
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
        self.assertEqual(app.pending_permissions, {})

    async def test_ask_human_times_out_and_returns_deny(self):
        app.permission_auto_expire_seconds = 0

        response = await app.permission_request_hook(
            FakeRequest(
                {
                    "session_id": "sess-2",
                    "hook_event_name": "PermissionRequest",
                    "tool_name": "Bash",
                    "tool_input": {
                        "command": "rm -rf node_modules",
                    },
                }
            )
        )

        payload = json.loads(response.body)
        self.assertEqual(payload["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertEqual(
            payload["hookSpecificOutput"]["decision"]["message"],
            "Permission request timed out",
        )
        self.assertEqual(len(app.pending_permissions), 1)
        perm = next(iter(app.pending_permissions.values()))
        self.assertEqual(perm["status"], "expired")

    async def test_user_response_unblocks_hook_and_returns_allow(self):
        task = asyncio.create_task(
            app.permission_request_hook(
                FakeRequest(
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

        await asyncio.sleep(0.05)
        self.assertEqual(len(app.pending_permissions), 1)
        perm_id, perm = next(iter(app.pending_permissions.items()))
        self.assertEqual(perm["status"], "pending")
        self.assertEqual(perm["description"], "Remove node_modules directory")
        self.assertEqual(
            perm["input_preview"],
            '{"command": "rm -rf node_modules", "description": "Remove node_modules directory"}',
        )

        await app.respond_permission(
            perm_id,
            FakeRequest({"key": "allow", "action": "approve"}),
        )
        response = await task

        payload = json.loads(response.body)
        self.assertEqual(payload["hookSpecificOutput"]["decision"]["behavior"], "allow")
        self.assertEqual(app.pending_permissions[perm_id]["status"], "approved")


if __name__ == "__main__":
    unittest.main()

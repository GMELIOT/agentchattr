import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
from permission_policy import PermissionPolicy
from telegram_notify import TelegramNotifier


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


class FakeTelegramNotifier:
    def __init__(self):
        self.answered: list[tuple[str, str]] = []
        self.updated: list[tuple[int, str, str]] = []

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        self.answered.append((callback_query_id, text))

    def update_permission_result(self, message_id: int, status: str, detail: str = "") -> None:
        self.updated.append((message_id, status, detail))

    def process_callback(self, callback_data: str) -> dict[str, str]:
        action, _, perm_id = callback_data.partition(":")
        if not action or not perm_id:
            raise ValueError("invalid callback_data")
        return {"action": action, "perm_id": perm_id}


class TelegramNotifierTests(unittest.TestCase):
    def test_process_callback_parses_action_and_permission_id(self):
        notifier = TelegramNotifier("token", "chat")

        parsed = notifier.process_callback("allow:perm-123")

        self.assertEqual(parsed, {"action": "allow", "perm_id": "perm-123"})

    def test_result_text_formats_status_and_detail(self):
        notifier = TelegramNotifier("token", "chat")

        text = notifier._result_text("approved", "Rule added: git\\ status")

        self.assertEqual(text, "✅ Approved\nRule added: git\\ status")

    def test_permission_pattern_for_auto_allow_escapes_current_action(self):
        pattern = app._permission_pattern_for_auto_allow(
            {"description": "git status --short", "tool_name": "Bash"}
        )

        self.assertEqual(pattern, r"git\ status\ \-\-short")

    def test_permission_option_key_prefers_deny_semantics_for_deny_action(self):
        key = app._permission_option_key(
            [
                {"key": "1", "label": "Approve"},
                {"key": "2", "label": "No, cancel"},
            ],
            "deny",
        )

        self.assertEqual(key, "2")


class TelegramCallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_notifier = app.telegram_notifier
        self.original_secret = app.telegram_callback_secret
        self.original_policy = app.permission_policy
        app.pending_permissions.clear()
        app.telegram_notifier = FakeTelegramNotifier()
        app.telegram_callback_secret = "cb-secret"
        app.permission_policy = PermissionPolicy(
            auto_allow=[],
            always_ask=[],
            dry_run=False,
            config_path=ROOT / "config.toml",
        )

    def tearDown(self):
        app.telegram_notifier = self.original_notifier
        app.telegram_callback_secret = self.original_secret
        app.permission_policy = self.original_policy
        app.pending_permissions.clear()

    async def test_always_callback_approves_permission_and_adds_rule(self):
        app.pending_permissions["perm1234"] = {
            "id": "perm1234",
            "agent": "claude",
            "action": "git status --short",
            "options": [{"key": "allow", "label": "Approve"}, {"key": "deny", "label": "Deny"}],
            "status": "pending",
            "key": "",
            "chosen_label": "",
            "created_at": 0,
            "tool_name": "Bash",
            "description": "git status --short",
            "telegram_message_id": 99,
        }

        response = await app.telegram_callback(
            "cb-secret",
            FakeRequest(
                {
                    "callback_query": {
                        "id": "cbq1",
                        "data": "always:perm1234",
                        "message": {"message_id": 99},
                    }
                }
            ),
        )

        self.assertEqual(response["status"], "approved")
        perm = app.pending_permissions["perm1234"]
        self.assertEqual(perm["status"], "approved")
        self.assertEqual(perm["auto_allow_pattern"], r"git\ status\ \-\-short")
        self.assertIn(r"git\ status\ \-\-short", app.permission_policy.get_rules()["auto_allow"])
        self.assertEqual(app.telegram_notifier.answered, [("cbq1", "Approved and rule added")])
        self.assertEqual(
            app.telegram_notifier.updated,
            [(99, "approved", r"Rule added: git\ status\ \-\-short")],
        )

    async def test_deny_callback_uses_deny_option_key(self):
        app.pending_permissions["perm5678"] = {
            "id": "perm5678",
            "agent": "claude",
            "action": "Run dangerous command",
            "options": [{"key": "1", "label": "Approve"}, {"key": "2", "label": "No, cancel"}],
            "status": "pending",
            "key": "",
            "chosen_label": "",
            "created_at": 0,
            "tool_name": "Bash",
            "description": "Run dangerous command",
            "telegram_message_id": 77,
        }

        response = await app.telegram_callback(
            "cb-secret",
            FakeRequest(
                {
                    "callback_query": {
                        "id": "cbq2",
                        "data": "deny:perm5678",
                        "message": {"message_id": 77},
                    }
                }
            ),
        )

        self.assertEqual(response["status"], "denied")
        perm = app.pending_permissions["perm5678"]
        self.assertEqual(perm["status"], "denied")
        self.assertEqual(perm["key"], "2")
        self.assertNotEqual(perm["key"], "1")
        self.assertEqual(app.telegram_notifier.answered[-1], ("cbq2", "Denied"))
        self.assertEqual(app.telegram_notifier.updated[-1], (77, "denied", ""))


if __name__ == "__main__":
    unittest.main()

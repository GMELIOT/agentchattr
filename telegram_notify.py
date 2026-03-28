"""Telegram Bot API helpers for permission delivery."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token.strip()
        self.chat_id = str(chat_id).strip()
        self.api = f"https://api.telegram.org/bot{self.bot_token}"

    def _post(self, method: str, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api}/{method}",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read())
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {data}")
        return data

    def _permission_text(self, perm_id: str, agent: str, tool_name: str, description: str) -> str:
        lines = [
            "Permission request",
            f"Agent: @{agent}",
        ]
        if tool_name:
            lines.append(f"Tool: {tool_name}")
        if description:
            lines.append(f"Action: {description}")
        lines.append(f"Request ID: {perm_id}")
        return "\n".join(lines)

    def _result_text(self, status: str, detail: str = "") -> str:
        prefix = {
            "approved": "✅ Approved",
            "denied": "❌ Denied",
            "expired": "⏰ Expired",
        }.get(status, status)
        return f"{prefix}\n{detail}".strip() if detail else prefix

    def send_permission_request(self, perm_id: str, agent: str, tool_name: str, description: str) -> int:
        payload = {
            "chat_id": self.chat_id,
            "text": self._permission_text(perm_id, agent, tool_name, description),
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {"text": "Approve", "callback_data": f"allow:{perm_id}"},
                        {"text": "Deny", "callback_data": f"deny:{perm_id}"},
                    ],
                    [
                        {"text": "Always Allow This Type", "callback_data": f"always:{perm_id}"},
                    ],
                ]
            },
        }
        data = self._post("sendMessage", payload)
        return int(data["result"]["message_id"])

    def update_permission_result(self, message_id: int, status: str, detail: str = "") -> None:
        payload = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": self._result_text(status, detail),
            "reply_markup": {"inline_keyboard": []},
        }
        self._post("editMessageText", payload)

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        payload = {"callback_query_id": callback_query_id}
        if text.strip():
            payload["text"] = text.strip()
        self._post("answerCallbackQuery", payload)

    def process_callback(self, callback_data: str) -> dict[str, str]:
        action, sep, perm_id = str(callback_data).partition(":")
        if not sep or not action.strip() or not perm_id.strip():
            raise ValueError("invalid callback_data")
        return {"action": action.strip(), "perm_id": perm_id.strip()}

    def set_webhook(self, callback_url: str) -> None:
        payload = {"url": callback_url}
        self._post("setWebhook", payload)

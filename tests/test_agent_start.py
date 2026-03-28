import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app


class _FakeRegistry:
    def __init__(self):
        self.register_calls: list[str] = []

    def get_base_config(self, base: str) -> dict | None:
        if base == "gemini":
            return {"command": "gemini", "label": "Gemini"}
        return None

    def get_instances_for(self, base: str) -> list[dict]:
        return []

    def register(self, base: str, label: str | None = None) -> dict | None:
        self.register_calls.append(base)
        if base != "gemini":
            return None
        return {
            "name": "gemini",
            "base": "gemini",
            "slot": 1,
            "label": "Gemini",
            "color": "#4285f4",
            "token": "token",
            "state": "active",
        }


class FakeRequest:
    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body


class AgentStartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_registry = app.registry
        self.original_store = app.store
        self.original_event_loop = app._event_loop
        self.original_tmux = app._tmux_session_exists
        self.original_start = app._start_agent_wrapper
        app.registry = _FakeRegistry()
        app.store = None
        app._event_loop = None
        app._starting_agents.clear()
        self.launches: list[str] = []
        app._tmux_session_exists = lambda session_name: False
        app._start_agent_wrapper = lambda base, cfg: self.launches.append(base)

    def tearDown(self):
        app.registry = self.original_registry
        app.store = self.original_store
        app._event_loop = self.original_event_loop
        app._tmux_session_exists = self.original_tmux
        app._start_agent_wrapper = self.original_start
        app._starting_agents.clear()

    async def test_second_start_is_blocked_while_first_is_in_progress(self):
        first = await app.start_agent("gemini")
        second = await app.start_agent("gemini")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(self.launches, ["gemini"])
        payload = json.loads(second.body)
        self.assertEqual(payload["error"], "agent start already in progress")

    async def test_registration_clears_in_progress_start_marker(self):
        first = await app.start_agent("gemini")
        self.assertEqual(first.status_code, 200)
        self.assertIn("gemini", app._starting_agents)

        response = await app.register_agent(FakeRequest({"base": "gemini"}))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("gemini", app._starting_agents)


if __name__ == "__main__":
    unittest.main()

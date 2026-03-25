import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import wrapper


class GeminiAutoKillTests(unittest.TestCase):
    def test_latest_sender_timestamp_matches_gemini_family(self):
        messages = [
            {"sender": "claude", "timestamp": 100.0},
            {"sender": "gemini-2", "timestamp": 150.0},
            {"sender": "gemini", "timestamp": 175.0},
        ]

        self.assertEqual(wrapper._latest_sender_timestamp(messages, "gemini"), 175.0)

    def test_latest_sender_timestamp_ignores_missing_timestamps(self):
        messages = [
            {"sender": "gemini", "timestamp": "bad"},
            {"sender": "gemini-3"},
            {"sender": "codex", "timestamp": 999.0},
        ]

        self.assertIsNone(wrapper._latest_sender_timestamp(messages, "gemini"))

    def test_should_auto_stop_when_both_message_and_commit_are_stale(self):
        self.assertTrue(
            wrapper._should_auto_stop(
                now=1_000.0,
                timeout_sec=600,
                latest_message_ts=300.0,
                last_commit_progress_ts=350.0,
            )
        )

    def test_should_not_auto_stop_when_message_is_recent(self):
        self.assertFalse(
            wrapper._should_auto_stop(
                now=1_000.0,
                timeout_sec=600,
                latest_message_ts=500.0,
                last_commit_progress_ts=None,
            )
        )

    def test_should_not_auto_stop_when_commit_is_recent(self):
        self.assertFalse(
            wrapper._should_auto_stop(
                now=1_000.0,
                timeout_sec=600,
                latest_message_ts=None,
                last_commit_progress_ts=500.0,
            )
        )


if __name__ == "__main__":
    unittest.main()

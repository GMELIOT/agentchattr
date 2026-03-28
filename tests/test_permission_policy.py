import logging
import tempfile
import unittest
from pathlib import Path

from permission_policy import PermissionPolicy


class PermissionPolicyTests(unittest.TestCase):
    def test_always_ask_overrides_auto_allow(self):
        policy = PermissionPolicy(
            auto_allow=[r"Read .+"],
            always_ask=[r"Read secret/.+"],
            dry_run=False,
        )

        decision = policy.evaluate("Read secret/token.txt")

        self.assertEqual(decision["decision"], "always_ask")
        self.assertEqual(decision["matched_rule"], r"Read secret/.+")

    def test_auto_allow_matches_when_not_dry_run(self):
        policy = PermissionPolicy(
            auto_allow=[r"Read .+"],
            always_ask=[],
            dry_run=False,
        )

        decision = policy.evaluate("Read src/app.py")

        self.assertEqual(decision["decision"], "auto_allow")
        self.assertEqual(decision["matched_rule"], r"Read .+")

    def test_unmatched_action_defaults_to_ask_human(self):
        policy = PermissionPolicy(
            auto_allow=[r"Read .+"],
            always_ask=[],
            dry_run=False,
        )

        decision = policy.evaluate("Write src/app.py")

        self.assertEqual(decision["decision"], "ask_human")
        self.assertIsNone(decision["matched_rule"])

    def test_dry_run_logs_but_still_asks_human(self):
        policy = PermissionPolicy(
            auto_allow=[r"Read .+"],
            always_ask=[],
            dry_run=True,
        )

        with self.assertLogs("permission_policy", level="INFO") as logs:
            decision = policy.evaluate("Read src/app.py")

        self.assertEqual(decision["decision"], "ask_human")
        self.assertEqual(decision["matched_rule"], r"Read .+")
        self.assertTrue(any("dry_run auto_allow" in line for line in logs.output))

    def test_add_auto_allow_persists_and_matches_subsequently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                "[permissions]\n"
                "dry_run = false\n"
                "auto_expire_seconds = 300\n"
                "auto_allow = [\n"
                "]\n"
                "always_ask = []\n",
                "utf-8",
            )
            policy = PermissionPolicy(
                auto_allow=[],
                always_ask=[],
                dry_run=False,
                config_path=config_path,
            )

            policy.add_auto_allow(r"Read new/.+")

            self.assertIn(r'"Read new/.+"', config_path.read_text("utf-8"))
            decision = policy.evaluate("Read new/file.txt")
            self.assertEqual(decision["decision"], "auto_allow")

    def test_case_insensitive_matching_works(self):
        policy = PermissionPolicy(
            auto_allow=[r"read .+"],
            always_ask=[],
            dry_run=False,
        )

        decision = policy.evaluate("READ src/App.py")

        self.assertEqual(decision["decision"], "auto_allow")

    def test_invalid_regex_in_config_is_skipped(self):
        with self.assertLogs("permission_policy", level="ERROR") as logs:
            policy = PermissionPolicy(
                auto_allow=[r"Read .+", r"["],
                always_ask=[],
                dry_run=False,
            )

        decision = policy.evaluate("Read src/app.py")
        self.assertEqual(decision["decision"], "auto_allow")
        self.assertTrue(any("invalid auto_allow regex" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()

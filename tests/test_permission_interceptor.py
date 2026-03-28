import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import wrapper
import wrapper_unix


def load_function_from_file(path: Path, name: str):
    source = path.read_text()
    module = ast.parse(source, filename=str(path))
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            isolated = ast.Module(body=[node], type_ignores=[])
            namespace = {}
            exec(compile(isolated, str(path), "exec"), namespace)
            return namespace[name]
    raise ValueError(f"Function {name} not found in {path}")


class PermissionPromptDetectionTests(unittest.TestCase):
    def test_codex_inline_options_are_parsed(self):
        pane = "\n".join([
            "Would you like to make the following edits?",
            "Apply (y) Skip (a) Cancel (esc)",
        ])

        prompt = wrapper_unix.detect_permission_prompt(pane)

        self.assertIsNotNone(prompt)
        self.assertEqual(prompt["agent_hint"], "codex")
        self.assertEqual(
            prompt["options"],
            [
                {"key": "y", "label": "Apply"},
                {"key": "a", "label": "Skip"},
                {"key": "esc", "label": "Cancel"},
            ],
        )

    def test_codex_run_command_prompt_uses_inline_key_hints(self):
        pane = "\n".join([
            "Would you like to run the following command?",
            "1. Yes, proceed (y)",
            "2. No, cancel (esc)",
        ])

        prompt = wrapper_unix.detect_permission_prompt(pane)

        self.assertIsNotNone(prompt)
        self.assertEqual(prompt["agent_hint"], "codex")
        self.assertEqual(
            prompt["options"],
            [
                {"key": "y", "label": "Yes, proceed (y)"},
                {"key": "esc", "label": "No, cancel (esc)"},
            ],
        )

    def test_multiline_claude_action_is_joined_before_options(self):
        pane = "\n".join([
            "Bash command",
            "",
            "Do you want to create this",
            "multi-line file?",
            "1. Yes",
            "2. No",
        ])

        prompt = wrapper_unix.detect_permission_prompt(pane)

        self.assertIsNotNone(prompt)
        self.assertEqual(prompt["agent_hint"], "claude")
        self.assertEqual(
            prompt["action"].split("\n\n")[0],
            "Do you want to create this multi-line file?",
        )
        self.assertEqual(
            prompt["options"],
            [
                {"key": "1", "label": "Yes"},
                {"key": "2", "label": "No"},
            ],
        )

    def test_claude_mcp_permission_prompt_is_detected(self):
        pane = "\n".join([
            'Allow the agentchattr MCP server to run tool "chat_read"?',
            "1. Allow once",
            "2. Allow for session",
            "3. Deny",
            "4. Cancel",
        ])

        prompt = wrapper_unix.detect_permission_prompt(pane)

        self.assertIsNotNone(prompt)
        self.assertEqual(prompt["agent_hint"], "claude")
        self.assertEqual(
            prompt["action"],
            'Allow the agentchattr MCP server to run tool "chat_read"?',
        )
        self.assertEqual(
            prompt["options"],
            [
                {"key": "1", "label": "Allow once"},
                {"key": "2", "label": "Allow for session"},
                {"key": "3", "label": "Deny"},
                {"key": "4", "label": "Cancel"},
            ],
        )

    def test_gemini_action_required_prompt_is_detected(self):
        pane = "\n".join([
            "Action Required",
            "",
            "Apply this change?",
            "1. Yes",
            "2. No",
        ])

        prompt = wrapper_unix.detect_permission_prompt(pane)

        self.assertIsNotNone(prompt)
        self.assertEqual(prompt["agent_hint"], "gemini")
        self.assertEqual(prompt["action"], "Action Required Apply this change?")
        self.assertEqual(
            prompt["options"],
            [
                {"key": "1", "label": "Yes"},
                {"key": "2", "label": "No"},
            ],
        )


class PermissionResponseHelperTests(unittest.TestCase):
    def test_fallback_permission_key_uses_first_available_option(self):
        self.assertEqual(
            wrapper._fallback_permission_key(
                [
                    {"key": "", "label": "Broken"},
                    {"key": "y", "label": "Apply"},
                    {"key": "a", "label": "Skip"},
                ]
            ),
            "y",
        )

    def test_chosen_permission_label_matches_key_case_insensitively(self):
        chosen_label = load_function_from_file(ROOT / "app.py", "_chosen_permission_label")
        self.assertEqual(
            chosen_label(
                [
                    {"key": "y", "label": "Apply"},
                    {"key": "esc", "label": "Cancel"},
                ],
                "ESC",
            ),
            "Cancel",
        )


if __name__ == "__main__":
    unittest.main()

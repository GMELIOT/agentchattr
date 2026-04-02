import tempfile
import unittest
from pathlib import Path

import app


class HatSanitizationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        app.config = {"server": {"data_dir": self._tmp.name}}
        app.agent_hats = {}
        app._event_loop = None

    def test_safe_hat_svg_is_stored(self):
        err = app.set_agent_hat(
            "codex",
            '<svg viewBox="0 0 32 16"><path d="M1 1 L2 2" fill="#fff"/></svg>',
        )

        self.assertIsNone(err)
        self.assertIn("codex", app.agent_hats)
        self.assertTrue((Path(self._tmp.name) / "hats.json").exists())

    def test_hat_svg_with_data_href_is_rejected(self):
        err = app.set_agent_hat(
            "codex",
            '<svg viewBox="0 0 32 16"><use href="data:text/html,<script>alert(1)</script>"/></svg>',
        )

        self.assertEqual(err, "Hat SVG contains unsupported or unsafe markup.")
        self.assertEqual(app.agent_hats, {})


if __name__ == "__main__":
    unittest.main()

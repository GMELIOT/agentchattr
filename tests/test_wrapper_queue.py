import tempfile
import unittest
from pathlib import Path

import wrapper


class WrapperQueueMigrationTests(unittest.TestCase):
    def test_migrate_queue_file_moves_unread_entries_to_new_identity_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            old_queue = data_dir / "claude_queue.jsonl"
            new_queue = data_dir / "claude-1_queue.jsonl"
            old_queue.write_text('{"text":"@claude test trigger"}\n', "utf-8")

            wrapper._migrate_queue_file(old_queue, new_queue)

            self.assertEqual(old_queue.read_text("utf-8"), "")
            self.assertEqual(
                new_queue.read_text("utf-8"),
                '{"text":"@claude test trigger"}\n',
            )

    def test_migrate_queue_file_appends_when_target_queue_already_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            old_queue = data_dir / "claude_queue.jsonl"
            new_queue = data_dir / "claude-1_queue.jsonl"
            old_queue.write_text('{"text":"second"}\n', "utf-8")
            new_queue.write_text('{"text":"first"}\n', "utf-8")

            wrapper._migrate_queue_file(old_queue, new_queue)

            self.assertEqual(
                new_queue.read_text("utf-8"),
                '{"text":"first"}\n{"text":"second"}\n',
            )


if __name__ == "__main__":
    unittest.main()

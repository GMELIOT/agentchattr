"""Tests for slack_bridge: prefix parser, thread tracker, and local logger."""

import os
import tempfile
import pytest
from pathlib import Path

from slack_bridge import (
    ParsedMessage,
    parse_protocol_message,
    ThreadTracker,
    InterTeamLogger,
    VALID_TAGS,
)


# ---------------------------------------------------------------------------
# parse_protocol_message
# ---------------------------------------------------------------------------

class TestParseProtocolMessage:
    def test_request_with_sender(self):
        text = "[REQUEST] Brand colour palette -- Galadriel @ 2026-04-02 10:00\nWe need the hex values for the primary palette.\nREF: design-vsem-v2"
        result = parse_protocol_message(text, channel="imladris-engineering", ts="1234.5")
        assert result is not None
        assert result.tag == "REQUEST"
        assert result.title == "Brand colour palette"
        assert "Galadriel" in result.sender
        assert "hex values" in result.body
        assert result.ref == "design-vsem-v2"
        assert result.channel == "imladris-engineering"
        assert result.ts == "1234.5"

    def test_response_tag(self):
        text = "[RESPONSE] Brand colour palette -- Claude @ 2026-04-02 10:05\nPrimary: #2E8B8B, Secondary: #D4784A"
        result = parse_protocol_message(text)
        assert result is not None
        assert result.tag == "RESPONSE"
        assert result.title == "Brand colour palette"
        assert "#2E8B8B" in result.body

    def test_update_tag(self):
        text = "[UPDATE] Phase 1 scaffold complete -- Claude @ 2026-04-02\nExpo + TypeScript + Supabase pushed to GitHub."
        result = parse_protocol_message(text)
        assert result is not None
        assert result.tag == "UPDATE"

    def test_blocker_tag(self):
        text = "[BLOCKER] Missing Supabase credentials -- Codex @ 2026-04-02\nCannot proceed without EU region project setup."
        result = parse_protocol_message(text)
        assert result is not None
        assert result.tag == "BLOCKER"
        assert "Supabase credentials" in result.title

    def test_fyi_tag(self):
        text = "[FYI] Sprint review tomorrow\nAll agents aware."
        result = parse_protocol_message(text)
        assert result is not None
        assert result.tag == "FYI"
        assert result.sender == ""  # No sender separator

    def test_no_tag_returns_none(self):
        assert parse_protocol_message("Hello, this is a normal message") is None
        assert parse_protocol_message("") is None
        assert parse_protocol_message("[INVALID] Not a real tag") is None

    def test_all_valid_tags_recognised(self):
        for tag in VALID_TAGS:
            text = f"[{tag}] Test title -- Test sender"
            result = parse_protocol_message(text)
            assert result is not None, f"Tag [{tag}] should be recognised"
            assert result.tag == tag

    def test_multiline_body(self):
        text = "[REQUEST] Design review -- Galadriel\nFirst line.\nSecond line.\nThird line."
        result = parse_protocol_message(text)
        assert result is not None
        assert "First line." in result.body
        assert "Third line." in result.body

    def test_en_dash_separator(self):
        text = "[REQUEST] Brand review \u2013 Galadriel @ 10:00"
        result = parse_protocol_message(text)
        assert result is not None
        assert result.title == "Brand review"
        assert "Galadriel" in result.sender

    def test_em_dash_separator(self):
        text = "[REQUEST] Brand review \u2014 Galadriel @ 10:00"
        result = parse_protocol_message(text)
        assert result is not None
        assert result.title == "Brand review"

    def test_thread_ts_passthrough(self):
        text = "[UPDATE] Status"
        result = parse_protocol_message(text, thread_ts="9999.0")
        assert result is not None
        assert result.thread_ts == "9999.0"

    def test_thread_ts_defaults_to_ts(self):
        text = "[UPDATE] Status"
        result = parse_protocol_message(text, ts="5555.0")
        assert result is not None
        assert result.thread_ts == "5555.0"

    def test_to_dict(self):
        text = "[FYI] Test"
        result = parse_protocol_message(text, channel="ops", ts="1.0")
        assert result is not None
        d = result.to_dict()
        assert d["tag"] == "FYI"
        assert d["channel"] == "ops"
        assert "raw" not in d


# ---------------------------------------------------------------------------
# ThreadTracker
# ---------------------------------------------------------------------------

class TestThreadTracker:
    def test_track_and_get(self):
        tracker = ThreadTracker()
        tracker.track("Brand colour palette", "1234.5678")
        assert tracker.get_thread("Brand colour palette") == "1234.5678"

    def test_case_insensitive(self):
        tracker = ThreadTracker()
        tracker.track("Brand Colour Palette", "1234.5678")
        assert tracker.get_thread("brand colour palette") == "1234.5678"
        assert tracker.get_thread("BRAND COLOUR PALETTE") == "1234.5678"

    def test_unknown_title_returns_none(self):
        tracker = ThreadTracker()
        assert tracker.get_thread("nonexistent") is None

    def test_overwrite(self):
        tracker = ThreadTracker()
        tracker.track("topic", "1111.0")
        tracker.track("topic", "2222.0")
        assert tracker.get_thread("topic") == "2222.0"

    def test_to_dict(self):
        tracker = ThreadTracker()
        tracker.track("Alpha", "1.0")
        tracker.track("Beta", "2.0")
        d = tracker.to_dict()
        assert len(d) == 2
        assert d["alpha"] == "1.0"

    def test_whitespace_stripped(self):
        tracker = ThreadTracker()
        tracker.track("  padded  ", "1.0")
        assert tracker.get_thread("padded") == "1.0"


# ---------------------------------------------------------------------------
# InterTeamLogger
# ---------------------------------------------------------------------------

class TestInterTeamLogger:
    def test_creates_file_with_header(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "log.md"
            logger = InterTeamLogger(log_path)
            logger.log_event("RECEIVED", "REQUEST", "Test title", "from Galadriel")
            content = log_path.read_text()
            assert "Timestamp" in content  # header
            assert "RECEIVED" in content
            assert "[REQUEST]" in content
            assert "Test title" in content

    def test_appends_multiple_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "log.md"
            logger = InterTeamLogger(log_path)
            logger.log_event("RECEIVED", "REQUEST", "First")
            logger.log_event("SENT", "RESPONSE", "Second")
            lines = log_path.read_text().strip().split("\n")
            # header (2 lines) + 2 events = 4 lines
            assert len(lines) == 4

    def test_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "sub" / "dir" / "log.md"
            logger = InterTeamLogger(log_path)
            logger.log_event("SENT", "FYI", "Nested test")
            assert log_path.exists()

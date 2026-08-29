import pytest
from datetime import datetime
from pathlib import Path

from activity_logger import (
    make_activity_line,
    make_log_header,
    parse_activity_line,
    ACTIVITY_PREFIX,
)


class TestActivityLogger:

    def test_make_log_header_has_shared_format(self):
        header = make_log_header("2026-08-28 10:00:00")

        assert header == (
            "SENTRY ACTIVITY LOG - 2026-08-28 10:00:00\n"
            "<< SENTRY ACTIVITY >>\n\n"
        )

    def test_make_activity_line_basic(self):
        command = "open_file"
        response = "success"
        timestamp = "2026-08-28 10:00:00"

        result = make_activity_line(command, response, timestamp=timestamp)

        assert result.startswith(ACTIVITY_PREFIX)
        assert command in result
        assert response in result
        assert timestamp in result

    def test_make_activity_line_with_source(self):
        command = "execute_command"
        response = "done"
        source = "keystroke_monitor"
        timestamp = "2026-08-28 10:00:00"

        result = make_activity_line(command, response, source=source, timestamp=timestamp)

        assert ACTIVITY_PREFIX in result
        assert source in result
        assert command in result
        assert response in result

    def test_make_activity_line_handles_none(self):
        result = make_activity_line(None, None)
        assert ACTIVITY_PREFIX in result

    def test_make_activity_line_handles_empty_strings(self):
        result = make_activity_line("", "")
        assert ACTIVITY_PREFIX in result

    def test_parse_activity_line_basic(self):
        timestamp = "2026-08-28 10:00:00"
        command = "test_command"
        response = "test_response"
        line = f"{ACTIVITY_PREFIX}|{timestamp}|{command}|{response}"

        result = parse_activity_line(line)

        assert result is not None
        assert result["timestamp"] == timestamp
        assert result["command"] == command
        assert result["feedback"] == response
        assert result["source"] == ""

    def test_parse_activity_line_with_source(self):
        timestamp = "2026-08-28 10:00:00"
        source = "mouse_monitor"
        command = "click_detected"
        response = "recorded"
        line = f"{ACTIVITY_PREFIX}|{timestamp}|{source}|{command}|{response}"

        result = parse_activity_line(line)

        assert result is not None
        assert result["timestamp"] == timestamp
        assert result["source"] == source
        assert result["command"] == command
        assert result["feedback"] == response

    def test_parse_activity_line_invalid_format(self):
        result = parse_activity_line("not_a_valid_line")
        assert result is None

    def test_parse_activity_line_missing_prefix(self):
        result = parse_activity_line("|2026-08-28|command|response")
        assert result is None

    def test_parse_activity_line_empty_input(self):
        result = parse_activity_line("")
        assert result is None

    def test_parse_activity_line_with_pipe_in_response(self):
        timestamp = "2026-08-28 10:00:00"
        source = "test_source"
        command = "test_cmd"
        response = "data|with|pipes"
        line = f"{ACTIVITY_PREFIX}|{timestamp}|{source}|{command}|{response}"

        result = parse_activity_line(line)

        assert result is not None
        assert result["feedback"] == response

    def test_make_and_parse_roundtrip(self):
        original_command = "test_action"
        original_response = "test_result"
        original_source = "test_module"
        timestamp = "2026-08-28 10:30:00"

        made_line = make_activity_line(
            original_command,
            original_response,
            source=original_source,
            timestamp=timestamp,
        )
        parsed = parse_activity_line(made_line)

        assert parsed is not None
        assert parsed["command"] == original_command
        assert parsed["feedback"] == original_response
        assert parsed["source"] == original_source
        assert parsed["timestamp"] == timestamp

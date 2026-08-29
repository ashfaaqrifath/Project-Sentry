import pytest
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from telegram_alert import (
    short_reasons,
    send_alert,
    send_drive_alert,
    telegram_alerts_enabled,
)


class TestTelegramAlert:

    def test_short_reasons_empty_list(self):
        result = short_reasons([])
        assert result == "pattern drift"

    def test_short_reasons_single_reason(self):
        result = short_reasons(["unusual_typing_speed"])
        assert "unusual_typing_speed" in result

    def test_short_reasons_multiple_reasons(self):
        reasons = ["reason_1", "reason_2", "reason_3"]
        result = short_reasons(reasons)

        assert "reason_1" in result
        assert "reason_2" in result
        assert "reason_3" in result

    def test_short_reasons_with_limit(self):
        reasons = ["reason_1", "reason_2", "reason_3", "reason_4", "reason_5"]
        result = short_reasons(reasons, limit=2)

        assert "reason_1" in result
        assert "reason_2" in result
        assert "+3 more" in result

    def test_short_reasons_with_whitespace(self):
        reasons = ["  reason_1  ", "  reason_2  ", ""]
        result = short_reasons(reasons)

        assert "reason_1" in result
        assert "reason_2" in result

    def test_short_reasons_limit_larger_than_list(self):
        reasons = ["reason_1", "reason_2"]
        result = short_reasons(reasons, limit=5)

        assert "reason_1" in result
        assert "reason_2" in result
        assert "+0 more" not in result

    @patch("telegram_alert.send_telegram_alert")
    def test_send_alert_message_format(self, mock_send):
        source = "keystroke"
        score = 0.85
        row_number = 42
        reasons = ["high_speed", "unusual_pattern"]

        send_alert(source, score, row_number, reasons)

        mock_send.assert_called_once()
        args = mock_send.call_args[0]
        message = args[1]

        assert "Anomaly detected" in message
        assert "85.0%" in message
        assert "42" in message
        assert "high_speed" in message

    @patch("telegram_alert.send_telegram_alert")
    def test_send_drive_alert_message_format(self, mock_send):
        drive = "C:"
        level = "critical"
        risk_score = 95
        reasons = ["high_temp", "error_rate_high"]

        send_drive_alert(drive, level, risk_score, reasons)

        mock_send.assert_called_once()
        args = mock_send.call_args[0]
        message = args[1]

        assert "CRITICAL" in message
        assert "C:" in message
        assert "95" in message
        assert "high_temp" in message

    @patch("telegram_alert.send_telegram_alert")
    def test_send_drive_alert_with_baseline(self, mock_send):
        drive = "D:"
        level = "warning"
        risk_score = 65
        reasons = ["increased_usage"]
        baseline = 45.5

        send_drive_alert(drive, level, risk_score, reasons, baseline_score=baseline)

        args = mock_send.call_args[0]
        message = args[1]

        assert "Baseline score: 45.5" in message

    def test_telegram_alerts_enabled_default_true(self, temp_dir):
        settings_file = temp_dir / "settings.json"
        settings_file.write_text("{}")

        with patch("telegram_alert.SETTINGS_FILE", str(settings_file)):
            result = telegram_alerts_enabled()
            assert result is True

    def test_telegram_alerts_enabled_true(self, temp_dir):
        settings_file = temp_dir / "settings.json"
        settings_file.write_text('{"telegram_alerts_enabled": true}')

        with patch("telegram_alert.SETTINGS_FILE", str(settings_file)):
            result = telegram_alerts_enabled()
            assert result is True

    def test_telegram_alerts_enabled_false(self, temp_dir):
        settings_file = temp_dir / "settings.json"
        settings_file.write_text('{"telegram_alerts_enabled": false}')

        with patch("telegram_alert.SETTINGS_FILE", str(settings_file)):
            result = telegram_alerts_enabled()
            assert result is False

    def test_telegram_alerts_enabled_missing_file(self):
        with patch("telegram_alert.SETTINGS_FILE", "/nonexistent/path/settings.json"):
            result = telegram_alerts_enabled()
            assert result is True

    def test_telegram_alerts_enabled_invalid_json(self, temp_dir):
        """Test when settings file has invalid JSON."""
        settings_file = temp_dir / "settings.json"
        settings_file.write_text("not valid json {")

        with patch("telegram_alert.SETTINGS_FILE", str(settings_file)):
            result = telegram_alerts_enabled()
            assert result is True

import pytest

from generate_report import (
    _number,
    _module_rows,
    COLORS,
)


class TestGenerateReport:

    def test_number_valid_integer(self):
        result = _number(42)
        assert result == 42.0
        assert isinstance(result, float)

    def test_number_valid_float(self):
        result = _number(3.14)
        assert result == 3.14

    def test_number_string_integer(self):
        result = _number("100")
        assert result == 100.0

    def test_number_string_float(self):
        result = _number("99.5")
        assert result == 99.5

    def test_number_invalid_string(self):
        result = _number("not_a_number")
        assert result == 0

    def test_number_none_returns_default(self):
        result = _number(None)
        assert result == 0

    def test_number_custom_default(self):
        result = _number("invalid", default=-1)
        assert result == -1

    def test_number_custom_default_with_valid_input(self):
        result = _number(42, default=-1)
        assert result == 42.0

    def test_module_rows_structure(self):
        summary = {
            "keystroke": {"anomalies_24h": 5},
            "mouse": {"anomalies_24h": 3},
            "network": {"anomalies_24h": 2},
            "drive": {"alerts": 1},
        }

        result = _module_rows(summary)

        assert len(result) == 4
        assert result[0][0] == "Keystroke dynamics"
        assert result[1][0] == "Mouse dynamics"
        assert result[2][0] == "Network usage"
        assert result[3][0] == "Drive health"

    def test_module_rows_with_missing_data(self):
        summary = {
            "keystroke": {"anomalies_24h": 5},
            "drive": {"alerts": 1},
        }

        result = _module_rows(summary)

        assert len(result) == 4
        assert result[0][0] == "Keystroke dynamics"
        assert result[1][1] == {}
        assert result[3][0] == "Drive health"

    def test_module_rows_empty_summary(self):
        result = _module_rows({})

        assert len(result) == 4
        for row in result:
            assert row[1] == {}

    def test_colors_defined(self):
        expected_keys = ["ink", "muted", "mint", "red", "amber", "pale"]

        for key in expected_keys:
            assert key in COLORS
            assert isinstance(COLORS[key], str)
            assert COLORS[key].startswith("#")

    def test_colors_are_valid_hex(self):
        for color_name, color_value in COLORS.items():
            assert color_value.startswith("#")
            assert len(color_value) == 7
            try:
                int(color_value[1:], 16)
            except ValueError:
                pytest.fail(f"Invalid hex color for {color_name}: {color_value}")

    def test_number_with_zero(self):
        result = _number(0)
        assert result == 0.0

    def test_number_with_negative(self):
        result = _number(-42.5)
        assert result == -42.5

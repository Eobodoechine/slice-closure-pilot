import pytest
from src.pilot_gate import validate_pilot_input

@pytest.mark.parametrize("input,expected", [
    ("", {"valid": False, "reason": "blank"}),
    ("   ", {"valid": False, "reason": "blank"}),
    ("test", {"valid": True, "value": "test"}),
])
def test_validate_pilot_input(input, expected):
    assert validate_pilot_input(input) == expected


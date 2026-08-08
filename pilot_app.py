"""Pilot slice for SLICE-PILOT-001 (slice-closure-gate compliant-path proof).

The gate contract pins `expect_substring: "def validate_pilot_input"` and
`test_cmd: "python3 -m pytest -q"`. This module supplies that binding symbol so a
legitimate slice turns the gate green, proving the gate passes real work (no
false-negative brick) and — under the v2 auto-merge posture — auto-merges on green.
"""


def validate_pilot_input(value):
    """Return the trimmed input string, or raise ValueError on None/empty input."""
    if value is None or str(value).strip() == "":
        raise ValueError("pilot input must be non-empty")
    return str(value).strip()

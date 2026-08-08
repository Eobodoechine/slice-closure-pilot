"""T3 hollow-slice probe for SLICE-PILOT-001.

This change is real (non-empty commit) and keeps `python3 -m pytest -q` green, but it
deliberately does NOT contain the contract-pinned symbol `def validate_pilot_input`.
Expected gate verdict: RED with reason `substring-absent` — proving "passing your own
tests is not enough" (the false-success defense).
"""


def some_unrelated_change(x):
    return x + 1

from pilot_payload import normalize_pilot_payload


def test_normalize_pilot_payload_trims_outer_whitespace():
    assert normalize_pilot_payload("  control  ") == "control"

from pilot_payload import normalize_pilot_payload


def test_normalizes():
    assert normalize_pilot_payload({"b": 2, "a": 1}) == {"a": 1, "b": 2}


def test_handles_none():
    assert normalize_pilot_payload(None) == {}


def test_normalizes_nested():
    assert normalize_pilot_payload({"z": 1, "a": {"k": "v"}}) == {"a": {"k": "v"}, "z": 1}

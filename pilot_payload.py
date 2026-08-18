"""Benign post-hardening control for the Gate S1 live proof."""


def normalize_pilot_payload(value: str) -> str:
    """Return a trimmed payload for the disposable pilot."""

    return value.strip()

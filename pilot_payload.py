def normalize_pilot_payload(payload):
    return {k: v for k, v in sorted((payload or {}).items())}

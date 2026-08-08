def validate_pilot_input(value):
    trimmed = value.strip()
    if not trimmed:
        return {"valid": False, "reason": "blank"}
    return {"valid": True, "value": trimmed}


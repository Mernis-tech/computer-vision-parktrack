from __future__ import annotations

import re

PHONE_VALIDATION_MESSAGE = "Номер телефона должен содержать 10-15 цифр и может начинаться с +"
_PHONE_ALLOWED_RE = re.compile(r"^\+?[0-9\s().-]+$")


def validate_optional_phone(value: str | None) -> str | None:
    if value is None:
        return None

    phone = value.strip()
    if not phone:
        return None

    digits = re.sub(r"\D", "", phone)
    if not _PHONE_ALLOWED_RE.fullmatch(phone) or not 10 <= len(digits) <= 15:
        raise ValueError(PHONE_VALIDATION_MESSAGE)

    return phone

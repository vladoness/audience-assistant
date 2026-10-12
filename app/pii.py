"""Masks personal data in free text before it reaches the model or the logs.

Audience requests should describe groups, not people, but marketers paste things. Anything
that looks like an email address, phone number or long numeric identifier is replaced with a
placeholder, and the count of replacements is recorded in telemetry.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("[EMAIL]", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("[IBAN]", re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?\b", re.IGNORECASE)),
    ("[ID]", re.compile(r"\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b", re.IGNORECASE)),  # IT codice fiscale
    ("[ID]", re.compile(r"\b[A-CEGHJ-PR-TW-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b", re.IGNORECASE)),  # UK NINO
    ("[ID]", re.compile(r"\b[XYZ]?\d{7,8}-?[A-Z]\b", re.IGNORECASE)),  # ES DNI / NIE
    ("[ID]", re.compile(r"\b[A-Z]{1,3}\d{6,}\b")),  # line / account ids
]
_PHONE = re.compile(r"(?<![\w-])\+?\d[\d\s().-]{6,}\d(?![\w-])")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def mask_pii(text: str) -> tuple[str, int]:
    """Return (masked_text, number_of_replacements)."""
    total = 0
    for placeholder, pattern in _PATTERNS:
        text, n = pattern.subn(placeholder, text)
        total += n

    def phone(match: re.Match[str]) -> str:
        nonlocal total
        candidate = match.group(0)
        if sum(ch.isdigit() for ch in candidate) < 8 or _ISO_DATE.match(candidate.strip()):
            return candidate
        total += 1
        return "[PHONE]"

    return _PHONE.sub(phone, text), total

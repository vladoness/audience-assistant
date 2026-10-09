"""Masks personal data in free text before it reaches the model or the logs.

Audience requests should describe groups, not people, but marketers paste things. Anything
that looks like an email address, phone number or long numeric identifier is replaced with a
placeholder, and the count of replacements is recorded in telemetry.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("[EMAIL]", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("[PHONE]", re.compile(r"(?<!\w)\+?\d[\d\s().-]{6,}\d(?!\w)")),
    ("[ID]", re.compile(r"\b[A-Z]{1,3}\d{6,}\b")),
]


def mask_pii(text: str) -> tuple[str, int]:
    """Return (masked_text, number_of_replacements)."""
    total = 0
    for placeholder, pattern in _PATTERNS:
        text, n = pattern.subn(placeholder, text)
        total += n
    return text, total

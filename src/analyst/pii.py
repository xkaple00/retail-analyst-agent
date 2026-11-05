"""Output-side PII masking. Second line of defence after the SQL column deny-list."""

from __future__ import annotations

import re

PII_COLUMNS = {
    "users": {"first_name", "last_name", "email", "street_address", "postal_code", "latitude", "longitude"},
}
SAFE_USER_COLUMNS = ["id", "age", "gender", "state", "city", "country", "traffic_source", "created_at"]

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("[email]", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    # Card: 13-19 digits in the usual groups of four. Phone: needs a +country, (area) or dashed/dotted
    # groups, so plain space-separated figures like "5 466 1234" in a sentence are left alone.
    ("[card]", re.compile(r"\b(?:\d{4}[ -]?){3}\d{1,7}\b")),
    ("[phone]", re.compile(
        r"(?<![\w.])(?:\+\d{1,3}[ -]?(?:\(\d{2,4}\)|\d{2,4})[ -]?\d{3,4}[ -]?\d{3,4}"
        r"|\(\d{2,4}\)[ -]?\d{3,4}[ -]?\d{3,4}"
        r"|\d{2,4}[-.]\d{3,4}[-.]\d{3,4})\b")),
    (
        "[address]",
        re.compile(
            r"\b\d{1,6}\s+(?:[A-Z][a-z]+\s){1,3}"
            r"(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Ln|Lane|Dr|Drive|Way|Ct|Court|Pl|Place)\b\.?",
        ),
    ),
    ("[coordinates]", re.compile(r"-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}")),
]


def mask_text(text: str) -> tuple[str, int]:
    """Return (masked_text, number_of_replacements)."""
    if not text:
        return text, 0
    total = 0
    for token, pat in _PATTERNS:
        text, n = pat.subn(token, text)
        total += n
    return text, total


def mask_value(value):
    """Mask strings inside nested tool results (lists/dicts/scalars)."""
    if isinstance(value, str):
        return mask_text(value)[0]
    if isinstance(value, list):
        return [mask_value(v) for v in value]
    if isinstance(value, dict):
        return {k: mask_value(v) for k, v in value.items()}
    return value

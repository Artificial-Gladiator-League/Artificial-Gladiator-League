"""Guard for free-text fields that must never hold payment or identity numbers.

Reviews and payouts record who did what and when, never document or account numbers.
"""
import re

from django.core.exceptions import ValidationError

# name@bank (a UPI id): a handle without a dotted domain, so ordinary emails do not match.
_UPI = re.compile(r"(?<![\w.\-])[\w.\-]{2,}@[A-Za-z][A-Za-z0-9]+\b(?!\.[A-Za-z])")
_PAN = re.compile(r"\b[A-Za-z]{5}\d{4}[A-Za-z]\b")
_LONG_DIGITS = re.compile(r"\d{9,}")

_CHECKS = (
    ("a UPI-style ID (name@bank)", _UPI),
    ("a PAN-style number (5 letters, 4 digits, 1 letter)", _PAN),
    ("a run of 9 or more digits (account, card, passport or phone-like number)", _LONG_DIGITS),
)


def sensitive_matches(text):
    """Descriptions of every sensitive pattern found in *text*."""
    return [label for label, pattern in _CHECKS if pattern.search(text or "")]


def validate_no_sensitive_data(value):
    found = sensitive_matches(value)
    if found:
        raise ValidationError(
            "This text looks like it contains %(found)s. Do not record bank, UPI, PAN, passport "
            "or other document numbers here: record who reviewed it and when, never the numbers.",
            code="sensitive_data",
            params={"found": " and ".join(found)},
        )

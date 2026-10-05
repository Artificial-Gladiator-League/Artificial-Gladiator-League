from django import forms

from .countries import normalize_country_codes, unknown_country_codes


class CountryCodesField(forms.CharField):
    """Comma-separated ISO 3166-1 alpha-2 codes, stored as a list (e.g. "IN" -> ["IN"])."""

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("help_text", "Comma-separated ISO 3166-1 alpha-2 codes, e.g. IL or IN. Leave empty for all countries.")
        super().__init__(**kwargs)

    def prepare_value(self, value):
        if isinstance(value, (list, tuple)):
            return ", ".join(value)
        return value

    def to_python(self, value):
        return normalize_country_codes(value or "")

    def validate(self, value):
        unknown = unknown_country_codes(value)
        if unknown:
            raise forms.ValidationError(f"Unknown country code(s): {', '.join(unknown)}.")

    def has_changed(self, initial, data):
        return normalize_country_codes(initial or []) != self.to_python(data)

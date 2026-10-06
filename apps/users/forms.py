import json
import logging
import re
import urllib.parse
import urllib.request

from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm

from .countries import country_choices, is_valid_country
from .models import CustomUser, GDPRRequest, validate_hf_repo_id


log = logging.getLogger(__name__)


def _get_recaptcha_field():
    """Return a hidden field to receive the reCAPTCHA v3 token injected by JS.

    The token is set by grecaptcha.execute() in register.html before submit.
    Server-side score validation happens in RegistrationForm.clean_captcha()
    via a direct call to Google's siteverify API — no third-party package needed.
    DEBUG mode skips verification because localhost always scores 0.0.
    """
    return forms.CharField(widget=forms.HiddenInput, required=False)

# Dark‑mode Tailwind attrs reused across all form widgets
_INPUT_CSS = (
    "w-full rounded-lg border border-gray-600 bg-gray-800 text-gray-100 "
    "placeholder-gray-500 px-4 py-2.5 focus:outline-none focus:ring-2 "
    "focus:ring-brand focus:border-brand transition"
)
_SELECT_CSS = _INPUT_CSS
_PASSWORD_CSS = _INPUT_CSS  # same styling, but used with PasswordInput


def _dark_attrs(extra=None, css=_INPUT_CSS, **kwargs):
    """Return a dict with Tailwind dark classes merged with any extras."""
    attrs = {"class": css}
    if extra:
        attrs.update(extra)
    attrs.update(kwargs)
    return attrs


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Registration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COUNTRY_WARNING = (
    "Your country of residence cannot be changed later. Choose carefully. "
    "To correct a mistake you will have to contact support."
)
COUNTRY_CONFIRM_LABEL = "I confirm this is my country of residence and I understand it cannot be changed later only after contacting support."
NAMES_CONFIRM_LABEL = "I understand that my username and my AG Champion name cannot be changed after registration."

class _UpperChoiceField(forms.ChoiceField):
    """ChoiceField that accepts the code in any case (the select always posts upper case)."""

    def to_python(self, value):
        return super().to_python((value or "").strip().upper())


class CountryChoiceMixin:
    """Required country select + confirmation checkbox, shared by registration and profile settings."""

    @staticmethod
    def build_country_fields():
        return {
            "country": _UpperChoiceField(
                required=True,
                label="Country of residence",
                choices=[("", "Select your country…")] + list(country_choices()),
                error_messages={"required": "Please select your country of residence."},
                widget=forms.Select(attrs={"class": "form-select"}),
                help_text=COUNTRY_WARNING,
            ),
            "country_confirm": forms.BooleanField(
                required=True,
                label=COUNTRY_CONFIRM_LABEL,
                error_messages={"required": "Please confirm your country of residence."},
                widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
            ),
        }

    def clean_country(self):
        code = (self.cleaned_data.get("country") or "").strip().upper()
        if not is_valid_country(code):
            raise forms.ValidationError("Please select your country of residence.")
        return code


class CountrySetForm(CountryChoiceMixin, forms.Form):
    """One-time country entry for existing accounts that have none (profile settings)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.update(self.build_country_fields())


class RegistrationForm(CountryChoiceMixin, forms.Form):
    """Username-only registration form with password confirmation and reCAPTCHA v3."""

    username = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Username",
            "autocomplete": "username",
        }),
    )
    password = forms.CharField(
        required=True,
        widget=forms.PasswordInput(attrs={
            "class": "form-control",
            "placeholder": "Password",
            "autocomplete": "new-password",
        }),
    )
    confirm_password = forms.CharField(
        required=True,
        label="Confirm password",
        widget=forms.PasswordInput(attrs={
            "class": "form-control",
            "placeholder": "Confirm password",
            "autocomplete": "new-password",
        }),
    )
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={
            "class": "form-control",
            "placeholder": "Email",
            "autocomplete": "email",
        }),
    )
    ai_name = forms.CharField(
        max_length=120,
        required=True,
        label="AI Name",
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "e.g. DeepPawn-v3",
        }),
        help_text="Display name for your AI bot. Cannot be changed after registration.",
    )
    names_confirm = forms.BooleanField(
        required=True,
        label=NAMES_CONFIRM_LABEL,
        error_messages={"required": "Please confirm that you understand your username and AG Champion name cannot be changed."},
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    captcha = _get_recaptcha_field()
    consent = forms.BooleanField(
        required=True,
        label="I agree to the Terms of Service and Privacy Policy",
        error_messages={"required": "You must accept the Terms of Service and Privacy Policy to register."},
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Rebuilt per instance so the field order is: ... ai_name, country, country_confirm, consent.
        extra = self.build_country_fields()
        ordered = {}
        for name, field in self.fields.items():
            if name == "consent":
                ordered.update(extra)
            ordered[name] = field
        self.fields = ordered

    def clean_captcha(self):
        """Verify the reCAPTCHA v3 token against Google's siteverify API."""
        token = self.cleaned_data.get("captcha", "")

        # RECAPTCHA_TESTING is True in DEBUG mode (set in settings.py) and can
        # also be forced via the RECAPTCHA_TESTING env var. When True, skip all
        # server-side validation so local dev and CI are never blocked.
        if getattr(settings, "RECAPTCHA_TESTING", False):
            return token

        # In production a token is mandatory — reject early before touching the secret.
        if not token:
            raise forms.ValidationError("reCAPTCHA verification failed. Please try again.")

        secret = getattr(settings, "RECAPTCHA_PRIVATE_KEY", "")
        if not secret:
            log.warning("RECAPTCHA_PRIVATE_KEY not set — skipping server-side reCAPTCHA check.")
            return token

        payload = urllib.parse.urlencode({"secret": secret, "response": token}).encode()
        try:
            with urllib.request.urlopen(
                "https://www.google.com/recaptcha/api/siteverify",
                data=payload,
                timeout=5,
            ) as resp:
                result = json.loads(resp.read().decode())
        except Exception as exc:
            log.warning("reCAPTCHA API error: %s", exc)
            raise forms.ValidationError("reCAPTCHA verification failed. Please try again.")

        if not result.get("success"):
            raise forms.ValidationError("reCAPTCHA verification failed. Please try again.")

        score = float(result.get("score", 0.0))
        required = float(getattr(settings, "RECAPTCHA_REQUIRED_SCORE", 0.5))
        if score < required:
            log.warning("reCAPTCHA score %.2f below threshold %.2f", score, required)
            raise forms.ValidationError("reCAPTCHA score too low. Please try again.")

        return token

    def clean_username(self):
        from django.contrib.auth.validators import UnicodeUsernameValidator
        username = self.cleaned_data.get("username", "").strip()
        if not username:
            raise forms.ValidationError("Username is required.")
        validator = UnicodeUsernameValidator()
        try:
            validator(username)
        except forms.ValidationError as exc:
            raise forms.ValidationError(exc.messages) from exc
        if CustomUser.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("This username is already taken.")
        return username

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if CustomUser.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean_ai_name(self):
        ai_name = self.cleaned_data.get("ai_name", "").strip()
        if CustomUser.objects.filter(ai_name__iexact=ai_name).exists():
            raise forms.ValidationError("This AI name is already taken. Please choose a different one.")
        return ai_name

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password", "")
        confirm_password = cleaned.get("confirm_password", "")
        if password and len(password) <= 8:
            self.add_error("password", "Password must be more than 8 characters.")
        if password and confirm_password and password != confirm_password:
            self.add_error("confirm_password", "Passwords do not match.")
        return cleaned


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Login (styled)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class StyledLoginForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"class": _INPUT_CSS, "placeholder": "Username"})
        self.fields["password"].widget.attrs.update({"class": _INPUT_CSS, "placeholder": "Password"})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Profile update
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fields that are permanently read-only after registration.
LOCKED_AFTER_REGISTRATION = (
    "username", "ai_name",
)

_LOCKED_MSG = "This field cannot be changed after registration."


class ProfileForm(forms.ModelForm):
    class Meta:
        model = CustomUser
        fields = (
            "username",
            "ai_name", "hf_model_repo_id",
        )
        widgets = {
            "username": forms.TextInput(attrs=_dark_attrs(placeholder="Username")),
            "ai_name": forms.TextInput(attrs=_dark_attrs(placeholder="e.g. DeepPawn‑v3")),
            "hf_model_repo_id": forms.TextInput(
                attrs=_dark_attrs(placeholder="e.g. Maxlegrec/ChessBot")
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # These fields are set at registration and permanently locked.
        if self.instance and self.instance.pk:
            for fname in LOCKED_AFTER_REGISTRATION:
                if fname in self.fields:
                    self.fields[fname].disabled = True
                    self.fields[fname].help_text = _LOCKED_MSG

    def clean_hf_model_repo_id(self):
        repo = self.cleaned_data.get("hf_model_repo_id", "").strip()
        validate_hf_repo_id(repo)
        return repo

    def clean(self):
        cleaned = super().clean()
        return cleaned


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Email change request
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_FORM_INPUT_CSS = "form-input w-full max-w-sm rounded-lg text-black placeholder-gray-500"


class EmailChangeRequestForm(forms.Form):
    new_email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={
            "class": _FORM_INPUT_CSS,
            "placeholder": "New email address",
            "autocomplete": "email",
        }),
    )
    confirm_new_email = forms.EmailField(
        required=True,
        label="Confirm new email",
        widget=forms.EmailInput(attrs={
            "class": _FORM_INPUT_CSS,
            "placeholder": "Confirm new email",
            "autocomplete": "email",
        }),
    )

    def clean_new_email(self):
        email = self.cleaned_data.get("new_email", "").strip().lower()
        if CustomUser.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean(self):
        cleaned = super().clean()
        new_email = cleaned.get("new_email", "")
        confirm = cleaned.get("confirm_new_email", "")
        if new_email and confirm and new_email.lower() != confirm.lower():
            self.add_error("confirm_new_email", "Email addresses do not match.")
        return cleaned


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Password change (no current-password check)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class SetNewPasswordForm(forms.Form):
    # Intentionally no current_password field — any active session can change the password.
    # Add a current_password field here to harden this if required later.
    new_password1 = forms.CharField(
        required=True,
        label="New password",
        widget=forms.PasswordInput(attrs={
            "class": _FORM_INPUT_CSS,
            "placeholder": "New password",
            "autocomplete": "new-password",
        }),
    )
    new_password2 = forms.CharField(
        required=True,
        label="Confirm your new password",
        widget=forms.PasswordInput(attrs={
            "class": _FORM_INPUT_CSS,
            "placeholder": "Confirm new password",
            "autocomplete": "new-password",
        }),
    )

    def clean(self):
        from django.contrib.auth.password_validation import validate_password
        cleaned = super().clean()
        p1 = cleaned.get("new_password1", "")
        p2 = cleaned.get("new_password2", "")
        if p1 and p2 and p1 != p2:
            self.add_error("new_password2", "Passwords do not match.")
        if p1:
            try:
                validate_password(p1)
            except forms.ValidationError as exc:
                self.add_error("new_password1", exc)
        return cleaned


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GDPR Data Access / Deletion Request
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class GDPRRequestForm(forms.ModelForm):
    class Meta:
        model = GDPRRequest
        fields = ("request_type", "reason")
        widgets = {
            "request_type": forms.Select(attrs=_dark_attrs(css=_SELECT_CSS)),
            "reason": forms.Textarea(attrs=_dark_attrs(
                placeholder="Optional — tell us why you're making this request.",
                rows="3",
            )),
        }
        labels = {
            "request_type": "Request type",
            "reason": "Reason (optional)",
        }

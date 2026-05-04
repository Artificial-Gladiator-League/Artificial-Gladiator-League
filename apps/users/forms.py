import logging
import re

from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

from .models import CustomUser, GDPRRequest

log = logging.getLogger(__name__)

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


def ensure_read_only_hf_token(hf_token: str) -> str:
    """Ensure *hf_token* is valid and does NOT grant write/admin permissions.

    Returns the HF username associated with the token. Raises
    ``forms.ValidationError`` on failure.
    """
    from huggingface_hub import HfApi
    from huggingface_hub.utils import HfHubHTTPError

    api = HfApi()
    try:
        whoami_result = api.whoami(token=hf_token)
        token_username = whoami_result.get("name") or whoami_result.get("login")
        if not token_username:
            raise forms.ValidationError({
                "hf_token": "Invalid Hugging Face access token (no username found).",
            })
    except HfHubHTTPError:
        raise forms.ValidationError({
            "hf_token": (
                "Invalid Hugging Face access token. "
                "Check that you copied the full token from https://huggingface.co/settings/tokens"
            ),
        })

    # Inspect token metadata and reject tokens with write/admin scopes.
    try:
        auth = whoami_result.get("auth") if isinstance(whoami_result, dict) else None
        access_token = None
        if isinstance(auth, dict):
            access_token = auth.get("accessToken")
        # Fallbacks for different HF whoami shapes
        if not access_token:
            access_token = whoami_result.get("token") or whoami_result.get("accessToken")

        if isinstance(access_token, dict):
            role = access_token.get("role") or access_token.get("permission")
            if role and isinstance(role, str):
                rl = role.lower()
                if "write" in rl or "admin" in rl or "owner" in rl or "maintain" in rl:
                    raise forms.ValidationError({
                        "hf_token": (
                            "Please provide a token with READ permissions only. "
                            "Tokens with write or admin scopes are not accepted."
                        ),
                    })

            scopes = access_token.get("scopes") or access_token.get("permissions") or []
            for s in scopes or []:
                try:
                    sval = str(s).lower()
                except Exception:
                    sval = ""
                if "write" in sval or "admin" in sval or "repo:write" in sval:
                    raise forms.ValidationError({
                        "hf_token": (
                            "Please provide a token with READ permissions only. "
                            "Tokens with write or admin scopes are not accepted."
                        ),
                    })
    except forms.ValidationError:
        raise
    except Exception:
        log.debug("Could not fully inspect token scopes from whoami(): %s", whoami_result)

    return token_username


# ────────────────────────────────────────────────────────────
#  Gated-repo validation helper
# ────────────────────────────────────────────────────────────
def validate_gated_hf_repo(repo_id: str, hf_token: str) -> None:
    from huggingface_hub import HfApi
    from huggingface_hub.utils import (
        GatedRepoError,
        HfHubHTTPError,
        RepositoryNotFoundError,
    )

    api = HfApi()

    # ── 1. Anonymous metadata lookup ─────────────────────────
    try:
        info = api.model_info(repo_id, token=False)
    except GatedRepoError:
        info = None
    except RepositoryNotFoundError:
        raise forms.ValidationError({
            "hf_model_repo_id":
                "This repository does not exist or is private. "
                "Check the repo ID (e.g. 'Maxlegrec/ChessBot').",
        })
    except HfHubHTTPError as exc:
        log.warning("HF metadata lookup failed for %s: %s", repo_id, exc)
        raise forms.ValidationError({
            "hf_model_repo_id":
                "Could not reach Hugging Face. Please try again in a moment.",
        })

    # ── 2. Reject public (non-gated) repos ───────────────────
    if info is not None:
        gated = getattr(info, "gated", None)
        if not gated or gated is False:
            raise forms.ValidationError({
                "hf_model_repo_id":
                    "Only gated repositories (with access requests enabled) are allowed. "
                    "Go to your repo Settings → enable 'Access Requests'.",
            })

    # ── 3. Validate token & get the actual HF username ───────
    try:
        whoami_result = api.whoami(token=hf_token)
        token_username = whoami_result.get("name")  # This is usually the username/login
        if not token_username:
            raise ValueError("whoami did not return a username")
        log.info("Token belongs to HF user: %s", token_username)
    except HfHubHTTPError:
        raise forms.ValidationError({
            "hf_token":
                "Invalid Hugging Face access token. "
                "Check that you copied the full token from "
                "https://huggingface.co/settings/tokens",
        })

    # ── 3b. Inspect token metadata and reject tokens with write/admin scopes
    # Some tokens (fine-grained access tokens) expose detailed metadata
    # under `whoami()` in `auth.accessToken` including `role` and `scopes`.
    # Reject any token that appears to grant write or admin permissions.
    try:
        auth = whoami_result.get("auth") if isinstance(whoami_result, dict) else None
        access_token = None
        if isinstance(auth, dict):
            access_token = auth.get("accessToken")
        # Fallbacks for different HF whoami shapes
        if not access_token:
            access_token = whoami_result.get("token") or whoami_result.get("accessToken")

        if isinstance(access_token, dict):
            # role is often a short string like 'read' / 'write' / 'admin'
            role = access_token.get("role") or access_token.get("permission")
            if role and isinstance(role, str):
                rl = role.lower()
                if "write" in rl or "admin" in rl or "owner" in rl or "maintain" in rl:
                    raise forms.ValidationError({
                        "hf_token": (
                            "Please provide a token with READ permissions only. "
                            "Tokens with write or admin scopes are not accepted."
                        ),
                    })

            # Also inspect any explicit scopes list (e.g. ['repo:read', 'repo:write'])
            scopes = access_token.get("scopes") or access_token.get("permissions") or []
            for s in scopes or []:
                try:
                    sval = str(s).lower()
                except Exception:
                    sval = ""
                if "write" in sval or "admin" in sval or "repo:write" in sval:
                    raise forms.ValidationError({
                        "hf_token": (
                            "Please provide a token with READ permissions only. "
                            "Tokens with write or admin scopes are not accepted."
                        ),
                    })
    except forms.ValidationError:
        raise
    except Exception:
        # Don't fail validation just because token metadata shape is unexpected.
        # In that case the token will be validated below by attempting to access the repo.
        log.debug("Could not fully inspect token scopes from whoami(): %s", whoami_result)

    # ── NEW: Enforce token user == repo owner/namespace ──────
    repo_namespace = repo_id.split("/")[0].strip()
    if token_username.lower() != repo_namespace.lower():
        raise forms.ValidationError({
            "hf_token":
                "This token does not belong to the owner of the specified "
                "repository. You must use your own Hugging Face token that "
                "matches the repo namespace. Create one at "
                "https://huggingface.co/settings/tokens",
        })

    # ── 4. Final access check (now we know it's the owner) ───
    try:
        api.model_info(repo_id, token=hf_token)
    except GatedRepoError:
        raise forms.ValidationError({
            "hf_token":
                "This token does not have approved access to the repository. "
                "Please request access on the model page and wait for approval.",
        })
    except RepositoryNotFoundError:
        raise forms.ValidationError({
            "hf_token":
                "This token does not have access to the repository.",
        })
    except HfHubHTTPError as exc:
        log.warning("HF token-auth check failed for %s: %s", repo_id, exc)
        exc_str = str(exc)
        if "403" in exc_str or "gated" in exc_str.lower():
            raise forms.ValidationError({
                "hf_token":
                    "Your token returned 403 Forbidden. If using a fine-grained token, "
                    "ensure it has 'Read access to contents of all public gated repos "
                    "you can access' enabled in https://huggingface.co/settings/tokens",
            })
        raise forms.ValidationError({
            "hf_token":
                "Could not verify access with the provided token. Please try again.",
        })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Registration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class RegistrationForm(UserCreationForm):
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs=_dark_attrs(placeholder="you@example.com")),
        help_text="Required. We'll send an activation link to verify your account.",
    )
    ai_name = forms.CharField(
        max_length=120,
        required=True,
        widget=forms.TextInput(attrs=_dark_attrs(placeholder="e.g. DeepPawn‑v3")),
        help_text="Display name for your AI bot.",
    )
    consent = forms.BooleanField(
        required=True,
        widget=forms.CheckboxInput(attrs={"class": "w-4 h-4 rounded border-gray-600 bg-gray-800 text-brand focus:ring-brand focus:ring-2 shrink-0 mt-0.5"}),
        error_messages={"required": "You must accept the Terms of Service and Privacy Policy to register."},
    )

    class Meta:
        model = CustomUser
        fields = (
            "username",
            "email",
            "ai_name",
            "password1",
            "password2",
        )

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if CustomUser.objects.filter(email=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean_ai_name(self):
        ai_name = self.cleaned_data.get("ai_name", "").strip()
        if CustomUser.objects.filter(ai_name__iexact=ai_name).exists():
            raise forms.ValidationError("This AI name is already taken. Please choose a different one.")
        return ai_name

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Apply dark styling to UserCreationForm's own widgets
        self.fields["username"].widget.attrs.update({"class": _INPUT_CSS, "placeholder": "Username"})
        self.fields["password1"].widget.attrs.update({"class": _INPUT_CSS, "placeholder": "Password"})
        self.fields["password2"].widget.attrs.update({"class": _INPUT_CSS, "placeholder": "Confirm password"})

    # ── Validation ──────────────────────────────
    def clean(self):
        cleaned = super().clean()
        return cleaned

    def save(self, commit=True):
        """Save the user WITHOUT the hf_token — it is never persisted."""
        user = super().save(commit=commit)
        # hf_token was used for validation only and is now discarded.
        return user


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
        if repo and not re.match(r'^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$', repo):
            raise forms.ValidationError(
                "Enter a valid Hugging Face repo ID (e.g. 'Maxlegrec/ChessBot')."
            )
        return repo

    def clean(self):
        cleaned = super().clean()
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

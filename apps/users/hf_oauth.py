# ──────────────────────────────────────────────
# apps/users/hf_oauth.py
#
# "Sign in with Hugging Face" — OAuth 2.0 / OIDC
#
# Flow
# ────
# 1. User clicks "Sign in with Hugging Face"
# 2. We redirect to HF's authorize endpoint with
#    a PKCE code_verifier + state stored in session
# 3. HF redirects back to our callback with a code
# 4. We exchange the code for an access token
# 5. We call /oauth/userinfo to get the HF username
# 6. We either log in an existing user or create a
#    new account linked to that HF identity
# 7. The OAuth token is Fernet-encrypted and stored
#    for future read-repos API calls — the raw
#    token never hits the database in plaintext.
#
# Scopes requested: openid, profile, read-repos
# (minimum needed for our use case)
#
# Existing email/password registration remains
# available as a fallback — this is additive.
# ──────────────────────────────────────────────
from __future__ import annotations

import base64
import hashlib
import logging
import secrets

import requests
from cryptography.fernet import Fernet

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from .models import CustomUser

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Token encryption helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _get_fernet() -> Fernet:
    """Derive a Fernet key from Django's SECRET_KEY.

    This keeps the OAuth token encrypted at rest. The raw token
    is only decrypted in memory when an API call is needed.
    """
    key_bytes = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key_bytes))


def encrypt_token(token: str) -> str:
    return _get_fernet().encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    return _get_fernet().decrypt(encrypted.encode()).decode()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PKCE helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _generate_pkce() -> tuple[str, str]:
    """Generate a PKCE code_verifier and code_challenge (S256)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Step 1 — Redirect to HF authorize endpoint
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def hf_oauth_start(request):
    """Initiate the HF OAuth flow — redirect to huggingface.co/oauth/authorize."""
    client_id = settings.HF_OAUTH_CLIENT_ID
    if not client_id:
        messages.error(request, "Hugging Face OAuth is not configured.")
        return redirect("users:login")

    # PKCE
    verifier, challenge = _generate_pkce()
    state = secrets.token_urlsafe(32)

    # Store in session for the callback to verify
    request.session["hf_oauth_state"] = state
    request.session["hf_oauth_verifier"] = verifier

    callback_url = request.build_absolute_uri(reverse("users:hf_oauth_callback"))

    params = {
        "client_id": client_id,
        "redirect_uri": callback_url,
        "response_type": "code",
        "scope": settings.HF_OAUTH_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorize_url = requests.Request(
        "GET", settings.HF_OAUTH_AUTHORIZE_URL, params=params
    ).prepare().url

    return redirect(authorize_url)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Step 2 — Handle the callback from HF
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def hf_oauth_callback(request):
    """Exchange the authorization code for an access token, then log in."""
    # ── Validate state ───────────────────────────────────────
    state = request.GET.get("state", "")
    saved_state = request.session.pop("hf_oauth_state", "")
    verifier = request.session.pop("hf_oauth_verifier", "")

    if not state or not secrets.compare_digest(state, saved_state):
        messages.error(request, "OAuth state mismatch. Please try again.")
        return redirect("users:login")

    code = request.GET.get("code", "")
    error = request.GET.get("error", "")
    if error or not code:
        messages.error(request, f"Hugging Face denied the request: {error or 'no code returned'}")
        return redirect("users:login")

    # ── Exchange code for token ──────────────────────────────
    callback_url = request.build_absolute_uri(reverse("users:hf_oauth_callback"))
    token_resp = requests.post(
        settings.HF_OAUTH_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": callback_url,
            "client_id": settings.HF_OAUTH_CLIENT_ID,
            "client_secret": settings.HF_OAUTH_CLIENT_SECRET,
            "code_verifier": verifier,
        },
        timeout=15,
    )
    if token_resp.status_code != 200:
        log.warning("HF token exchange failed: %s %s", token_resp.status_code, token_resp.text)
        messages.error(request, "Could not complete Hugging Face sign-in. Please try again.")
        return redirect("users:login")

    token_data = token_resp.json()
    access_token = token_data.get("access_token", "")
    if not access_token:
        messages.error(request, "No access token received from Hugging Face.")
        return redirect("users:login")

    # ── Fetch userinfo ───────────────────────────────────────
    userinfo_resp = requests.get(
        settings.HF_OAUTH_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if userinfo_resp.status_code != 200:
        log.warning("HF userinfo failed: %s", userinfo_resp.status_code)
        messages.error(request, "Could not retrieve your Hugging Face profile.")
        return redirect("users:login")

    userinfo = userinfo_resp.json()
    hf_username = userinfo.get("preferred_username") or userinfo.get("name", "")
    if not hf_username:
        messages.error(request, "Could not determine your Hugging Face username.")
        return redirect("users:login")

    log.info("HF OAuth: userinfo for %s retrieved successfully", hf_username)

    # ── Find or create the Django user ───────────────────────
    encrypted = encrypt_token(access_token)
    now = timezone.now()

    # Case 1: Existing user already linked to this HF account
    try:
        user = CustomUser.objects.get(hf_username=hf_username)
        user.hf_oauth_token_encrypted = encrypted
        user.hf_oauth_linked_at = now
        user.save(update_fields=["hf_oauth_token_encrypted", "hf_oauth_linked_at"])
        # Attempt to preload models but do not block login on failure
        try:
            from apps.games.model_preloader import preload_user_models
            try:
                preload_user_models(user.pk)
            except Exception as e:
                log.exception("Preload failed (non-critical) for existing OAuth user %s: %s", user.pk, e)
        except Exception:
            log.exception("Model preload import failed during OAuth callback for user %s", user.pk)

        # Trigger snapshot download for all registered models in a background thread
        import threading as _threading
        def _bg_sync_case1():
            try:
                from apps.users.models import UserGameModel
                from apps.users.model_lifecycle import sync_hf_repo_to_local
                for gm in UserGameModel.objects.filter(user=user, hf_model_repo_id__gt=""):
                    try:
                        sync_hf_repo_to_local(user, gm, token=access_token)
                    except Exception:
                        log.exception("OAuth Case1: sync_hf_repo_to_local failed for user=%s game=%s", user.pk, gm.game_type)
            except Exception:
                log.exception("OAuth Case1: background sync failed for user=%s", user.pk)
        _threading.Thread(target=_bg_sync_case1, daemon=True, name=f"hf-oauth-{user.pk}").start()

        login(request, user)
        messages.success(request, f"Welcome back, {user.username}!")
        return redirect("games:lobby")
    except CustomUser.DoesNotExist:
        pass

    # Case 2: Logged-in user linking their HF account
    if request.user.is_authenticated:
        user = request.user
        user.hf_username = hf_username
        user.hf_oauth_token_encrypted = encrypted
        user.hf_oauth_linked_at = now
        user.save(update_fields=[
            "hf_username",
            "hf_oauth_token_encrypted",
            "hf_oauth_linked_at",
        ])
        # Attempt to preload models for linked user; don't block linking on failure
        try:
            from apps.games.model_preloader import preload_user_models
            try:
                preload_user_models(user.pk)
            except Exception as e:
                log.exception("Preload failed (non-critical) when linking HF account for user %s: %s", user.pk, e)
        except Exception:
            log.exception("Model preload import failed when linking HF account for user %s", user.pk)

        # Trigger snapshot download in background (non-blocking)
        import threading as _threading
        def _bg_sync_case2():
            try:
                from apps.users.models import UserGameModel
                from apps.users.model_lifecycle import sync_hf_repo_to_local
                for gm in UserGameModel.objects.filter(user=user, hf_model_repo_id__gt=""):
                    try:
                        sync_hf_repo_to_local(user, gm, token=access_token)
                    except Exception:
                        log.exception("OAuth Case2: sync_hf_repo_to_local failed for user=%s game=%s", user.pk, gm.game_type)
            except Exception:
                log.exception("OAuth Case2: background sync failed for user=%s", user.pk)
        _threading.Thread(target=_bg_sync_case2, daemon=True, name=f"hf-link-{user.pk}").start()

        messages.success(request, f"Your Hugging Face account ({hf_username}) has been linked.")
        return redirect("users:profile")

    # Case 3: New user — store info in session, send to completion form
    request.session["hf_oauth_pending"] = {
        "hf_username": hf_username,
        "access_token_encrypted": encrypted,
    }
    return redirect("users:hf_oauth_complete")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Step 3 — Complete registration for new OAuth users
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def hf_oauth_complete(request):
    """Let a new OAuth user pick a username, AI name, and repo ID.

    The HF token is already stored (encrypted) from the callback —
    the user does NOT need to paste it manually.
    """
    pending = request.session.get("hf_oauth_pending")
    if not pending:
        messages.error(request, "No pending OAuth session. Please sign in again.")
        return redirect("users:login")

    hf_username = pending["hf_username"]
    encrypted_token = pending["access_token_encrypted"]

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        ai_name = request.POST.get("ai_name", "").strip()
        repo_id = request.POST.get("hf_model_repo_id", "").strip()
        email = request.POST.get("email", "").strip().lower()

        errors = {}
        if not username:
            errors["username"] = "Username is required."
        elif CustomUser.objects.filter(username=username).exists():
            errors["username"] = "This username is already taken."
        if not ai_name:
            errors["ai_name"] = "AI name is required."
        if not repo_id:
            errors["hf_model_repo_id"] = "Model repo ID is required."
        elif CustomUser.objects.filter(hf_model_repo_id=repo_id).exists():
            errors["hf_model_repo_id"] = (
                "This model repository is already registered by another "
                "user. Each account must use a unique model."
            )
        elif repo_id.split("/")[0].strip().lower() != hf_username.lower():
            errors["hf_model_repo_id"] = (
                "You can only register a model repository that belongs to "
                "your own Hugging Face account."
            )
        if email and CustomUser.objects.filter(email=email).exists():
            errors["email"] = "An account with this email already exists."

        if errors:
            return render(request, "users/hf_oauth_complete.html", {
                "hf_username": hf_username,
                "errors": errors,
                "form_data": request.POST,
            })

        # Create the user — no password needed for OAuth-only accounts,
        # but set_unusable_password() so Django doesn't complain.
        user = CustomUser(
            username=username,
            email=email,
            ai_name=ai_name,
            hf_model_repo_id=repo_id,
            hf_username=hf_username,
            hf_oauth_token_encrypted=encrypted_token,
            hf_oauth_linked_at=timezone.now(),
            is_active=True,  # OAuth-verified users are active immediately
        )
        user.set_unusable_password()
        user.save()

        # Create a UserGameModel for chess and pin the model revision
        if repo_id:
            from .models import UserGameModel
            gm, _ = UserGameModel.objects.get_or_create(
                user=user,
                game_type="chess",
                defaults={"hf_model_repo_id": repo_id},
            )
            try:
                raw_token = decrypt_token(encrypted_token)
                from .integrity import record_original_sha
                record_original_sha(gm, raw_token)
            except Exception:
                log.exception("Could not pin model for OAuth user %s", username)

            # Verify committed/cached files exist — no download triggered
            try:
                from apps.games.local_inference import verify_local_files
                ok, msg = verify_local_files(gm)
                if ok:
                    log.info("Local model files verified for OAuth user %s", username)
                else:
                    log.warning("Local model files not found for OAuth user %s: %s", username, msg)
            except Exception:
                log.exception("Model verification failed for OAuth user %s", username)

        # Clear the pending session data
        request.session.pop("hf_oauth_pending", None)

        # Attempt to preload models before completing login, but do not block
        try:
            from apps.games.model_preloader import preload_user_models
            try:
                preload_user_models(user.pk)
            except Exception as e:
                log.exception("Preload failed (non-critical) for new OAuth user %s: %s", user.pk, e)
        except Exception:
            log.exception("Model preload import failed for new OAuth user %s", user.pk)

        login(request, user)
        messages.success(request, f"Welcome to Artificial Gladiator, {username}!")
        return redirect("games:lobby")

    return render(request, "users/hf_oauth_complete.html", {
        "hf_username": hf_username,
        "errors": {},
        "form_data": {},
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helper: get a user's decrypted HF token
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_user_hf_token(user: CustomUser) -> str | None:
    """Return the decrypted HF OAuth token, or None if not linked."""
    if not user.hf_oauth_token_encrypted:
        return None
    try:
        return decrypt_token(user.hf_oauth_token_encrypted)
    except Exception:
        log.warning("Could not decrypt HF token for %s", user.username)
        return None

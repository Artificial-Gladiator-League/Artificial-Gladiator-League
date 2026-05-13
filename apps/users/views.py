import json
import logging
import shutil
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import make_password
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView, LogoutView
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views.generic import UpdateView

from .tokens import account_activation_token

from .forms import GDPRRequestForm, ProfileForm, RegistrationForm, StyledLoginForm
from .models import CustomUser, GDPRRequest, UserGameModel
from apps.games.models import Game
from apps.tournaments.models import Match

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  User Search (autocomplete for navbar)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@login_required
def user_search(request):
    """Return up to 8 users matching ?q= by username or AI name."""
    q = request.GET.get("q", "").strip()
    if len(q) < 2:
        return JsonResponse({"results": []})

    from django.db.models import Q
    users = (
        CustomUser.objects
        .filter(Q(username__icontains=q) | Q(ai_name__icontains=q))
        .exclude(pk=request.user.pk)
        .only("pk", "username", "elo")[:8]
    )

    friend_ids = set(request.user.friends.values_list("pk", flat=True))

    results = [
        {
            "username": u.username,
            "elo": u.elo,
            "profile_url": f"/users/profile/@{u.username}/",
            "is_friend": u.pk in friend_ids,
            "fide_abbr": u.get_fide_title().get("abbr", ""),
            "fide_css": u.get_fide_title().get("css", ""),
            "fide_title": u.get_fide_title().get("title", ""),
        }
        for u in users
    ]
    return JsonResponse({"results": results})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Stat helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_TC_CATEGORIES = [
    {"label": "Bullet", "icon": "⚡", "css": "bullet", "max_base": 2},
    {"label": "Blitz",  "icon": "🔥", "css": "blitz",  "max_base": 9},
    {"label": "Rapid",  "icon": "🕐", "css": "rapid",  "max_base": 99999},
]


def _tc_category(tc_str: str) -> dict:
    """Map a time-control string like '3+1' to a category dict."""
    try:
        base = int(tc_str.split("+")[0])
    except (ValueError, IndexError, AttributeError):
        base = 3  # default to blitz
    for cat in _TC_CATEGORIES:
        if base <= cat["max_base"]:
            return cat
    return _TC_CATEGORIES[-1]


def _compute_stats(entries: list) -> dict:
    """Given a list of dicts with 'user_won' and 'is_draw' keys, return stats."""
    wins = sum(1 for e in entries if e["user_won"])
    draws = sum(1 for e in entries if e["is_draw"])
    losses = sum(1 for e in entries if not e["user_won"] and not e["is_draw"])
    total = wins + losses + draws
    win_rate = round(wins / total, 4) if total else 0.0
    # Compute current streak from most-recent games
    streak = 0
    for e in entries:  # entries should already be sorted newest-first
        if e["user_won"]:
            if streak >= 0:
                streak += 1
            else:
                break
        elif e["is_draw"]:
            break
        else:
            if streak <= 0:
                streak -= 1
            else:
                break
    return {
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "total": total,
        "win_rate": win_rate,
        "win_rate_pct": round(win_rate * 100),
        "streak": streak,
    }


def _category_for_elo(elo: int) -> dict:
    """Return a category dict for a given ELO value (mirrors CustomUser.get_category)."""
    if elo >= 2700:
        return {"tier": "Super Grandmaster", "icon": "👑", "css": "text-yellow-300"}
    elif elo >= 2500:
        return {"tier": "Grandmaster",       "icon": "🏆", "css": "text-purple-400"}
    elif elo >= 2400:
        return {"tier": "International Master", "icon": "🥇", "css": "text-yellow-400"}
    elif elo >= 2300:
        return {"tier": "FIDE Master",       "icon": "🥈", "css": "text-blue-300"}
    elif elo >= 2200:
        return {"tier": "Candidate Master",  "icon": "🥈", "css": "text-gray-300"}
    elif elo >= 2000:
        return {"tier": "Expert",            "icon": "🥇", "css": "text-orange-400"}
    elif elo >= 1800:
        return {"tier": "Class A",           "icon": "🔴", "css": "text-red-400"}
    elif elo >= 1600:
        return {"tier": "Class B",           "icon": "🟠", "css": "text-orange-300"}
    elif elo >= 1400:
        return {"tier": "Class C",           "icon": "🟡", "css": "text-yellow-500"}
    elif elo >= 1200:
        return {"tier": "Class D",           "icon": "🟢", "css": "text-green-400"}
    else:
        return {"tier": "Beginner",          "icon": "🥉", "css": "text-amber-600"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Registration — username-only, rate-limited, reCAPTCHA v3
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _get_client_ip(request):
    """Extract the real client IP from the request."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _ratelimit_decorator():
    """Return the ratelimit decorator if django-ratelimit is installed, else identity."""
    try:
        from ratelimit.decorators import ratelimit
        return ratelimit(key="ip", rate="5/h", method="POST", block=False)
    except ImportError:
        return lambda f: f


def register(request):
    if request.user.is_authenticated:
        return redirect("users:profile")

    form = RegistrationForm(request.POST or None)

    if request.method == "POST":
        ip = _get_client_ip(request)

        if getattr(request, "limited", False):
            log.warning("Registration rate-limited for IP %s", ip)
            form.add_error(None, "Too many registration attempts from your IP. Please try again later.")
        elif form.is_valid():
            username = form.cleaned_data["username"]
            password = form.cleaned_data["password"]
            ai_name = form.cleaned_data["ai_name"]
            user = CustomUser(username=username, ai_name=ai_name, is_active=True)
            user.password = make_password(password)
            user.save()
            log.info("New user registered: %s (AI: %s, IP: %s)", username, ai_name, ip)
            login(request, user)
            return redirect("users:profile")
        else:
            # Log form errors to aid debugging (e.g. empty RECAPTCHA_PUBLIC_KEY)
            log.debug(
                "Registration form invalid for IP %s — errors: %s",
                ip,
                form.errors.as_json(),
            )

            # Surface a clear error if reCAPTCHA specifically failed
            if "captcha" in form.errors:
                log.warning(
                    "reCAPTCHA validation failed for IP %s. "
                    "Check that RECAPTCHA_PUBLIC_KEY and RECAPTCHA_PRIVATE_KEY are "
                    "set correctly in your environment. Get keys at: "
                    "https://www.google.com/recaptcha/admin/create",
                    ip,
                )
                form.add_error(None, "Invalid CAPTCHA - bots not allowed.")

    return render(request, "users/register.html", {
        "form": form,
        "recaptcha_site_key": settings.RECAPTCHA_PUBLIC_KEY,
    })


# Apply rate-limiting at module load time so the decorator wraps the function
register = _ratelimit_decorator()(register)


def activation_sent(request):
    """Confirmation page shown after registration."""
    return render(request, "users/activation_sent.html")


def activate(request, uidb64, token):
    """Validate the activation token and activate the account (single-use)."""
    try:
        uid = urlsafe_base64_decode(uidb64).decode()
        user = CustomUser.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, CustomUser.DoesNotExist):
        user = None

    if user is not None and account_activation_token.check_token(user, token):
        # Atomic: only activates if still inactive — prevents race conditions
        updated = CustomUser.objects.filter(pk=user.pk, is_active=False).update(
            is_active=True,
            last_model_validation_date=timezone.now().date(),
        )
        if updated:
            user.refresh_from_db()
            # Preload models before finalizing login (non-blocking).
            try:
                from apps.games.model_preloader import preload_user_models
                try:
                    preload_user_models(user.pk)
                except Exception as e:
                    log.exception("Preload failed (non-critical) during activation for user %s: %s", user.pk, e)
            except Exception:
                log.exception("Model preload import failed during activation for user %s", user.pk)

            login(request, user)
            messages.success(request, "Your account has been activated! Welcome.", extra_tags="activation")
            return redirect("games:lobby")

    messages.error(
        request,
        "This activation link is invalid or has already been used.",
    )
    return redirect("users:login")


def _check_duplicate_ip(ip: str, new_user):
    """Basic multi-account detection: warn if IP matches existing accounts.

    Production: expand with Redis rate-limiting and device fingerprinting.
    Currently only logs a warning for admin review — does not block.
    """
    from django.contrib.sessions.models import Session
    # Simple heuristic — check last_login IP stored via session middleware
    # For now, just log for admin review
    log.info(
        "Registration IP check: user=%s ip=%s",
        new_user.username, ip,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AI Models (per-game configuration)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _check_repo_is_gated(repo_id: str):
    """Return an error string if *repo_id* fails either gating or platform-access checks.

    Two checks are performed in order:
      1. The repo must be gated (access-request enabled).  An anonymous
         model_info call that raises GatedRepoError confirms this.
      2. The platform account (ArtificialGladiatorLeague) must have been
         granted access.  We verify by retrying model_info with the platform
         token; a second GatedRepoError means the user hasn't approved us yet.

    Returns None when both checks pass.
    """
    try:
        from huggingface_hub import HfApi
        from huggingface_hub.utils import GatedRepoError, RepositoryNotFoundError, HfHubHTTPError

        api = HfApi()

        # ── 1. Confirm the repo is gated ─────────────────────
        try:
            info = api.model_info(repo_id, token=False)
            # Anonymous call succeeded — check the gated flag explicitly
            if not getattr(info, "gated", None):
                return (
                    "Only gated repositories (with access requests enabled) are accepted. "
                    "Go to your repo Settings on Hugging Face and enable 'Access Requests', "
                    "then try again."
                )
            # gated flag present — fall through to platform-access check
        except GatedRepoError:
            pass  # anonymous blocked → repo is gated, as required
        except RepositoryNotFoundError:
            return f"Repository '{repo_id}' was not found on Hugging Face. Check the repo ID and make sure it is public."
        except HfHubHTTPError as exc:
            log.warning("HF API error checking gated status for %s: %s", repo_id, exc)
            return "Could not reach Hugging Face to verify the repository. Please try again in a moment."

        # ── 2. Confirm platform account has approved access ───
        _NOT_APPROVED = (
            "Required: Grant Access to Our Platform Account — "
            "your repo must be Gated and you must approve 'ArtificialGladiatorLeague' "
            "in the Access Settings of your Hugging Face repo, "
            "otherwise your submission will be rejected."
        )
        platform_token = settings.HF_PLATFORM_TOKEN or None
        if platform_token:
            try:
                api.model_info(repo_id, token=platform_token)
                # Success — platform account has access
            except GatedRepoError:
                # Explicitly a gated-access denial
                return _NOT_APPROVED
            except HfHubHTTPError as exc:
                # HF sometimes raises a plain 403 HfHubHTTPError instead of
                # the GatedRepoError subclass when a token is present but not
                # approved — treat any 403 as "not approved".
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 403:
                    return _NOT_APPROVED
                log.warning("Platform token access check failed for %s: %s", repo_id, exc)
                # Fail open for other HTTP errors (5xx, timeouts, etc.)
            except RepositoryNotFoundError as exc:
                log.warning("Platform token access check — repo not found for %s: %s", repo_id, exc)
                # Fail open
        else:
            log.warning("HF_PLATFORM_TOKEN not set — skipping platform-access check for %s", repo_id)

        return None

    except Exception as exc:
        log.warning("Unexpected error in _check_repo_is_gated for %s: %s", repo_id, exc)
        return None  # fail open so a transient error doesn't permanently block submission


GAME_TYPES = [
    {"type": "chess", "label": "Chess"},
    {"type": "breakthrough", "label": "Breakthrough"},
]


@login_required
def ai_models(request):
    """Manage per-game AI model configurations.

    GET            → show current models with status for each game.
    POST (connect) → register a repo_id; generate a challenge code.
    POST (verify)  → check AGL_VERIFY.txt in the repo and mark is_verified.
    """
    import re
    from .ownership_verification import check_full_ownership, generate_verification_code

    if request.method == "POST":
        action = request.POST.get("action", "connect")
        game_type = request.POST.get("game_type", "").strip()
        repo_id = request.POST.get("hf_model_repo_id", "").strip()
        data_repo_id = request.POST.get("hf_data_repo_id", "").strip()

        if game_type not in [g["type"] for g in GAME_TYPES]:
            messages.error(request, "Invalid game type.")
            return redirect("users:ai_models")

        # ── Verify ownership (check AGL_VERIFY.txt) ──────────
        if action == "verify":
            submitted_repo_id = request.POST.get("hf_model_repo_id", "").strip()
            gm = request.user.get_game_model(game_type)
            if not gm:
                messages.error(request, "No model registered for this game type yet.")
            elif submitted_repo_id and submitted_repo_id != gm.hf_model_repo_id:
                # Submitted repo_id doesn't match the one stored for this game_type.
                # This prevents a code generated for one repo/game from verifying another.
                messages.error(
                    request,
                    f"Repository mismatch: the submitted repo '{submitted_repo_id}' does not "
                    f"match the registered {game_type} repo '{gm.hf_model_repo_id}'. "
                    "Please reconnect the correct repo first.",
                )
            else:
                ok, msg = check_full_ownership(gm)
                if ok:
                    messages.success(request, msg)
                else:
                    messages.error(request, msg)
            return redirect("users:ai_models")

        # ── Connect / update a repo ───────────────────────────
        if not repo_id:
            messages.error(request, "Please enter a Hugging Face repo ID.")
        elif not re.match(r'^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$', repo_id):
            messages.error(request, "Invalid repo ID format (e.g. 'YourName/MyModel').")
        elif data_repo_id and not re.match(r'^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$', data_repo_id):
            messages.error(request, "Invalid data repo ID format (e.g. 'YourName/breakthrough-data').")
        elif (_gated_err := _check_repo_is_gated(repo_id)) is not None:
            messages.error(request, _gated_err)
        else:
            # Check for duplicate repo across other users
            dup = UserGameModel.objects.filter(
                hf_model_repo_id=repo_id,
            ).exclude(user=request.user)
            if dup.exists():
                messages.error(
                    request,
                    "This model repository is already registered by another user.",
                )
            else:
                gm, created = UserGameModel.objects.get_or_create(
                    user=request.user,
                    game_type=game_type,
                    defaults={"hf_model_repo_id": repo_id, "hf_data_repo_id": data_repo_id},
                )
                if not created and (gm.hf_model_repo_id != repo_id or gm.hf_data_repo_id != data_repo_id):
                    # Repo changed — reset verification
                    gm.hf_model_repo_id = repo_id
                    gm.hf_data_repo_id = data_repo_id
                    gm.is_verified = False
                    gm.verification_code = ""
                    gm.save(update_fields=[
                        "hf_model_repo_id", "hf_data_repo_id",
                        "is_verified", "verification_code",
                    ])

                from .ownership_verification import VERIFY_FILENAME
                label = dict((g["type"], g["label"]) for g in GAME_TYPES).get(game_type, game_type)
                if gm.is_verified:
                    # Repo unchanged and already verified — just confirm, no code reset
                    messages.success(request, f"{label} repo saved (already verified).")
                else:
                    # New repo or repo changed — issue a challenge code
                    code = generate_verification_code(gm)
                    messages.info(
                        request,
                        f"{label} repo saved. To prove ownership, create '{VERIFY_FILENAME}' "
                        f"at the root of {repo_id} containing exactly: {code} — "
                        "then click 'Verify Ownership' below. "
                        "If you have an HF Space or data repo, add the same file to those roots too.",
                    )

        return redirect("users:ai_models")

    # Build config list for each game type
    game_configs = []
    for g in GAME_TYPES:
        gm = request.user.get_game_model(g["type"])
        game_configs.append({
            "type": g["type"],
            "label": g["label"],
            "model": gm,
        })

    return render(request, "users/ai_models.html", {
        "game_configs": game_configs,
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Login / Logout
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class UserLoginView(LoginView):
    template_name = "users/login.html"
    authentication_form = StyledLoginForm
    redirect_authenticated_user = True

    def form_valid(self, form):
        # Attempt to preload models before finalizing login, but do not block.
        user = getattr(form, "get_user", lambda: None)()
        try:
            if user and getattr(user, "pk", None) is not None:
                from apps.games.model_preloader import preload_user_models, ensure_user_folders
                try:
                    preload_user_models(user.pk)
                except Exception as e:
                    # Log a warning and ensure minimal folders exist so login proceeds.
                    log.warning("Preload failed for user %s: %s", getattr(user, "pk", None), e)
                    try:
                        ensure_user_folders(user.pk)
                    except Exception:
                        log.exception("ensure_user_folders failed for user %s", getattr(user, "pk", None))
        except Exception:
            log.exception("Model preload import failed during login for user %s", getattr(user, "pk", None))

        # Proceed with regular login now that models are preloaded
        response = super().form_valid(form)

        # Best-effort: create a per-session live models directory for this user.
        try:
            user = getattr(self.request, "user", None)
            if user and getattr(user, "pk", None) is not None:
                live_root = getattr(settings, "LIVE_MODELS_ROOT", None)
                if live_root:
                    live_dir = Path(live_root) / f"user_{user.pk}"
                    live_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        (live_dir / ".placeholder").write_text("Live models dir\n")
                    except Exception:
                        # Non-fatal; directory existence is the important part
                        pass
        except Exception:
            log.exception("Failed to create live models directory for user on login")

        return response


class UserLogoutView(LogoutView):
    next_page = "/"

    def dispatch(self, request, *args, **kwargs):
        # Best-effort: remove the per-session live models directory for this user.
        try:
            user = getattr(request, "user", None)
            if user and getattr(user, "pk", None) is not None:
                live_root = getattr(settings, "LIVE_MODELS_ROOT", None)
                if live_root:
                    live_dir = Path(live_root) / f"user_{user.pk}"
                    if live_dir.exists():
                        shutil.rmtree(live_dir, ignore_errors=True)
        except Exception:
            log.exception("Failed to remove live models directory for user on logout")

        # Logout cleanup is also scheduled by the user_logged_out signal in
        # apps.users.signals which captures the user id and schedules
        # `cleanup_on_logout` asynchronously.
        # As a best-effort additional cleanup, remove preloaded models now.
        try:
            if user and getattr(user, "pk", None) is not None:
                from apps.games.model_preloader import clear_user_models
                try:
                    clear_user_models(user.pk)
                except Exception:
                    log.exception("Best-effort clear_user_models failed for user %s", user.pk)
        except Exception:
            log.exception("Could not perform best-effort clear_user_models")
        return super().dispatch(request, *args, **kwargs)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public Profile (read-only, for viewing other users)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@login_required
def public_profile(request, username):
    """View another user's public profile with friend/chat actions."""
    profile_user = get_object_or_404(CustomUser, username=username)
    # If viewing own profile, redirect to the edit version
    if profile_user == request.user:
        return redirect("users:profile")

    # Compute friendship status for the social buttons
    # Social features removed — no friend or chat actions
    friendship_status = "none"
    friend_request_id = None

    return render(request, "users/profile.html", {
        "user_profile": profile_user,
        "category": profile_user.get_category(),
        "category_chess": _category_for_elo(profile_user.elo_chess),
        "category_breakthrough": _category_for_elo(profile_user.elo_breakthrough),
        "is_own_profile": False,
        "friendship_status": friendship_status,
        "friend_request_id": friend_request_id,
        "friends_list": profile_user.friends.only("pk", "username", "elo", "last_login"),
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Breakthrough file-status helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _build_breakthrough_file_status(user, gm):
    """Fetch config_model.json and config_data.json to build file status lists.

    Uses the exact pinned revision from the UserGameModel:
      1. ``approved_full_sha`` (immutable commit)
      2. ``submitted_ref`` (branch/tag chosen at submission)
      3. ``"main"`` as last resort
    """
    import json as _json
    from apps.users.hf_oauth import get_user_hf_token

    model_files = []
    data_files = []
    model_error = ""
    data_error = ""
    found_any = False

    # For profile display we use ONLY the platform-level read token
    # (HF_PLATFORM_TOKEN) rather than any user's personal token.
    # This ensures we only use a read-only token when rendering
    # profile.html.
    platform_token = settings.HF_PLATFORM_TOKEN or None
    hf_data_repo_id = gm.hf_data_repo_id or f"{user.username}/{gm.game_type}-data"

    # Determine the correct revision for this user's model
    revision = gm.approved_full_sha or gm.submitted_ref or "main"

    # Model repo (gated — needs token)
    try:
        from huggingface_hub import HfApi, hf_hub_download
        from huggingface_hub.utils import (
            EntryNotFoundError,
            GatedRepoError,
            HfHubHTTPError,
            RepositoryNotFoundError,
            LocalEntryNotFoundError,
        )

        api = HfApi()
        # Use only the platform token (read-only) for profile checks.
        tokens_to_try = [platform_token] if platform_token else [None]

        repo_files = None
        token_used = None
        last_exc = None

        for tkn in tokens_to_try:
            try:
                repo_files = api.list_repo_files(
                    repo_id=gm.hf_model_repo_id,
                    revision=revision,
                    token=tkn,
                )
                token_used = tkn
                break
            except RepositoryNotFoundError as exc:
                # Could be not found or lack of access with this token — try next
                last_exc = exc
                continue
            except HfHubHTTPError as exc:
                # Revision not found or permission issue — try next token
                last_exc = exc
                # If it's clearly a revision-not-found, expose that to the caller
                exc_str = str(exc)
                if "404" in exc_str or "RevisionNotFound" in exc_str:
                    raise EntryNotFoundError(
                        f"Revision '{revision}' not found in repo '{gm.hf_model_repo_id}'."
                    )
                continue

        if repo_files is None:
            # Could not access the repo with any token
            model_error = f"Model repo '{gm.hf_model_repo_id}' not found or you lack access."
            # If we have a platform token, try to determine the platform username
            try:
                if platform_token:
                    platform_api = HfApi(token=platform_token)
                    who = platform_api.whoami()
                    plat_name = (who and (who.get("name") or who.get("login"))) or None
                else:
                    plat_name = None
            except Exception:
                plat_name = None

            if plat_name:
                model_error += f" Please add '{plat_name}' to your authorized users on Hugging Face, or link your Hugging Face account to this site."
            else:
                if not platform_token:
                    model_error += (
                        " Server configuration: HF_PLATFORM_TOKEN is not set on this server. "
                        "Set the environment variable HF_PLATFORM_TOKEN to a Hugging Face token for the platform account "
                        "(the account that was granted access) and restart the server."
                    )
                else:
                    model_error += " Please link your Hugging Face account to this site or contact an administrator to grant platform access."

            log.warning("Could not list repo files for %s (user=%s): %s", gm.hf_model_repo_id, user.username, last_exc)
        else:
            if "config_model.json" not in repo_files:
                model_error = (
                    f"config_model.json not found in '{gm.hf_model_repo_id}' "
                    f"at revision '{revision[:12]}…'."
                )
                log.warning(
                    "config_model.json missing from file listing: repo=%s rev=%s files=%s",
                    gm.hf_model_repo_id, revision, repo_files[:20],
                )
            else:
                config_path = hf_hub_download(
                    repo_id=gm.hf_model_repo_id,
                    filename="config_model.json",
                    token=token_used,
                    revision=revision,
                )
                with open(config_path, "r") as fh:
                    config = _json.load(fh)
                expected = config.get("files", [])
                # Mark each expected file as present/absent based on the
                # repo file listing we retrieved above.
                present_map = [{"name": f, "present": (f in (repo_files or []))} for f in expected]
                missing = [m["name"] for m in present_map if not m["present"]]
                if missing:
                    # Show a clear textual error to the user when any
                    # listed file is missing (template will display
                    # model_error when model_files is empty).
                    model_error = (
                        "The model repo is missing files listed in config_model.json: "
                        + ", ".join(missing)
                        + "."
                    )
                    model_files = []
                else:
                    model_files = present_map
                found_any = True

    except GatedRepoError:
        # Platform token exists but was not granted access to this gated repo.
        try:
            if platform_token:
                _who = HfApi(token=platform_token).whoami() or {}
                plat_name = _who.get("name") or _who.get("login")
            else:
                plat_name = None
        except Exception:
            plat_name = None
        account_hint = f" Please add '{plat_name}' to the list of approved users on your Hugging Face repo." if plat_name else " Please grant access to the platform account on your Hugging Face repo."
        model_error = f"Access denied (403) for repo '{gm.hf_model_repo_id}'.{account_hint}"
        log.warning("GatedRepoError for %s (user=%s) — platform token present but not approved", gm.hf_model_repo_id, user.username)
    except RepositoryNotFoundError:
        model_error = f"Model repo '{gm.hf_model_repo_id}' not found or you lack access."
        log.warning("Repo not found: %s (user=%s)", gm.hf_model_repo_id, user.username, exc_info=True)
    except EntryNotFoundError as e:
        # File or revision not found
        model_error = str(e) or f"config_model.json not found at revision '{revision[:12]}…'."
        log.warning("Entry not found: repo=%s rev=%s user=%s", gm.hf_model_repo_id, revision, user.username, exc_info=True)
    except LocalEntryNotFoundError as e:
        # Hugging Face could not be reached and the file isn't in the local cache.
        # This commonly happens when the server has no outbound network access
        # or when a proxy/firewall blocks requests to huggingface.co.
        model_error = (
            "Could not reach Hugging Face to download model files and they are not present in the local cache. "
            "Please check the server's network/proxy settings and ensure it can reach https://huggingface.co, "
            "or pre-download the files on this machine using `hf_hub_download` and retry."
        )
        log.exception("LocalEntryNotFoundError when fetching model files: repo=%s user=%s", gm.hf_model_repo_id, user.username)
    except HfHubHTTPError as e:
        model_error = f"HuggingFace API error reading model repo (HTTP {getattr(e, 'response', None) and e.response.status_code})."
        log.exception("HfHub HTTP error: repo=%s rev=%s user=%s", gm.hf_model_repo_id, revision, user.username)
    except _json.JSONDecodeError:
        model_error = "config_model.json exists but contains invalid JSON."
        log.warning("Invalid JSON in config_model.json: repo=%s rev=%s", gm.hf_model_repo_id, revision)
    except Exception:
        model_error = "Unexpected error reading config_model.json from model repo."
        log.exception("Unexpected error loading config_model.json: repo=%s rev=%s user=%s", gm.hf_model_repo_id, revision, user.username)

    # Data repo (public — no token needed, always uses main)
    try:
        from huggingface_hub import HfApi, hf_hub_download
        from huggingface_hub.utils import (
            EntryNotFoundError,
            HfHubHTTPError,
            RepositoryNotFoundError,
        )

        # Download the data config and then verify each listed file exists
        # in the public dataset repo.
        config_path = hf_hub_download(
            repo_id=hf_data_repo_id,
            filename="config_data.json",
            repo_type="dataset",
            token=None,
        )
        with open(config_path, "r") as fh:
            config = _json.load(fh)
        expected = config.get("files", [])
        # List dataset repo files (public) and check presence
        try:
            data_api = HfApi()
            data_repo_files = data_api.list_repo_files(repo_id=hf_data_repo_id, repo_type="dataset", token=None)
        except Exception:
            data_repo_files = None

        present_map = [{"name": f, "present": (f in (data_repo_files or []))} for f in expected]
        missing = [m["name"] for m in present_map if not m["present"]]
        if missing:
            data_error = (
                "The data repo is missing files listed in config_data.json: "
                + ", ".join(missing)
                + "."
            )
            data_files = []
        else:
            data_files = present_map
        found_any = True
    except RepositoryNotFoundError:
        data_error = f"Data repo '{hf_data_repo_id}' not found."
        log.warning("Data repo not found: %s (user=%s)", hf_data_repo_id, user.username)
    except EntryNotFoundError:
        data_error = f"config_data.json not found in data repo '{hf_data_repo_id}'."
        log.warning("config_data.json missing: repo=%s user=%s", hf_data_repo_id, user.username)
    except LocalEntryNotFoundError:
        data_error = (
            "Could not reach Hugging Face to download data files and they are not present in the local cache. "
            "Please check the server's network/proxy settings and ensure it can reach https://huggingface.co, "
            "or pre-download the files on this machine using `hf_hub_download` and retry."
        )
        log.exception("LocalEntryNotFoundError when fetching data files: repo=%s user=%s", hf_data_repo_id, user.username)
    except HfHubHTTPError:
        data_error = "HuggingFace API error reading data repo."
        log.exception("HfHub HTTP error: data repo=%s user=%s", hf_data_repo_id, user.username)
    except _json.JSONDecodeError:
        data_error = "config_data.json exists but contains invalid JSON."
        log.warning("Invalid JSON in config_data.json: repo=%s", hf_data_repo_id)
    except Exception:
        data_error = "Unexpected error reading config_data.json from data repo."
        log.exception("Unexpected error loading config_data.json: repo=%s user=%s", hf_data_repo_id, user.username)

    if not found_any and not model_error and not data_error:
        return None

    return {
        "model_files": model_files,
        "data_files": data_files,
        "model_error": model_error,
        "data_error": data_error,
        "revision_used": revision,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Profile (UpdateView)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class ProfileView(LoginRequiredMixin, UpdateView):
    model = CustomUser
    form_class = ProfileForm
    template_name = "users/profile.html"
    success_url = reverse_lazy("users:profile")

    def get_object(self, queryset=None):
        return self.request.user

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        ctx["user_profile"] = user
        ctx["category"] = user.get_category()

        # Per-game categories derived from each game's ELO independently
        ctx["category_chess"] = _category_for_elo(user.elo_chess)
        ctx["category_breakthrough"] = _category_for_elo(user.elo_breakthrough)

        ctx["is_own_profile"] = True
        ctx["fields_locked"] = True
        ctx["friends_list"] = user.friends.only("pk", "username", "elo", "last_login")

        # ── Tournament match history ───────────────────────
        matches_as_p1 = Match.objects.filter(
            player1=user,
            match_status=Match.MatchStatus.COMPLETED,
            is_armageddon=False,
        ).select_related("player2", "tournament")
        matches_as_p2 = Match.objects.filter(
            player2=user,
            match_status=Match.MatchStatus.COMPLETED,
            is_armageddon=False,
        ).select_related("player1", "tournament")

        tournament_entries = []
        for m in matches_as_p1:
            tc_cat = _tc_category(m.time_control)
            tournament_entries.append({
                "replay_id": m.pk,
                "replay_type": "match",
                "tournament_id": m.tournament_id,
                "tournament_name": m.tournament.name,
                "round_num": m.round_num,
                "opponent": m.player2.username,
                "opponent_flag": m.player2.country_flag,
                "result": m.result,
                "user_won": m.winner_id == user.pk,
                "is_draw": m.result == "1/2-1/2",
                "date": m.timestamp,
                "time_control": m.time_control,
                "tc_css": tc_cat["css"],
                "tc_label": tc_cat["label"],
                "game_type": getattr(m.tournament, "game_type", "chess") or "chess",
            })
        for m in matches_as_p2:
            tc_cat = _tc_category(m.time_control)
            tournament_entries.append({
                "replay_id": m.pk,
                "replay_type": "match",
                "tournament_id": m.tournament_id,
                "tournament_name": m.tournament.name,
                "round_num": m.round_num,
                "opponent": m.player1.username,
                "opponent_flag": m.player1.country_flag,
                "result": m.result,
                "user_won": m.winner_id == user.pk,
                "is_draw": m.result == "1/2-1/2",
                "date": m.timestamp,
                "time_control": m.time_control,
                "tc_css": tc_cat["css"],
                "tc_label": tc_cat["label"],
                "game_type": getattr(m.tournament, "game_type", "chess") or "chess",
            })
        tournament_entries.sort(key=lambda h: h["date"], reverse=True)

        # ── One-on-one (casual) game history ───────────────
        COMPLETED = [
            Game.Status.WHITE_WINS,
            Game.Status.BLACK_WINS,
            Game.Status.DRAW,
            Game.Status.TIMEOUT_WHITE,
            Game.Status.TIMEOUT_BLACK,
        ]
        casual_as_white = Game.objects.filter(
            white=user,
            is_tournament_game=False,
            status__in=COMPLETED,
        ).select_related("black")
        casual_as_black = Game.objects.filter(
            black=user,
            is_tournament_game=False,
            status__in=COMPLETED,
        ).select_related("white")

        oneonone_entries = []
        for g in casual_as_white:
            tc_cat = _tc_category(g.time_control)
            is_draw = g.result == "1/2-1/2"
            user_won = g.winner_id == user.pk
            oneonone_entries.append({
                "replay_id": g.pk,
                "replay_type": "game",
                "tournament_id": None,
                "tournament_name": "",
                "round_num": "",
                "opponent": g.black.username if g.black else "?",
                "opponent_flag": g.black.country_flag if g.black else "",
                "result": g.result,
                "user_won": user_won,
                "is_draw": is_draw,
                "date": g.timestamp,
                "time_control": g.time_control,
                "tc_css": tc_cat["css"],
                "tc_label": tc_cat["label"],
                "game_type": g.game_type,
            })
        for g in casual_as_black:
            tc_cat = _tc_category(g.time_control)
            is_draw = g.result == "1/2-1/2"
            user_won = g.winner_id == user.pk
            oneonone_entries.append({
                "replay_id": g.pk,
                "replay_type": "game",
                "tournament_id": None,
                "tournament_name": "",
                "round_num": "",
                "opponent": g.white.username if g.white else "?",
                "opponent_flag": g.white.country_flag if g.white else "",
                "result": g.result,
                "user_won": user_won,
                "is_draw": is_draw,
                "date": g.timestamp,
                "time_control": g.time_control,
                "tc_css": tc_cat["css"],
                "tc_label": tc_cat["label"],
                "game_type": g.game_type,
            })
        oneonone_entries.sort(key=lambda h: h["date"], reverse=True)

        # ── Merged history (tournament + one-on-one) ───────
        all_history = tournament_entries + oneonone_entries
        all_history.sort(key=lambda h: h["date"], reverse=True)
        ctx["match_history"] = all_history[:100]

        # ── Format stats for Stats & History tab ───────────
        def _build_format_block(label, icon, entries):
            """Build a format stats dict with overall + per-time-control breakdown."""
            overall = _compute_stats(entries)
            # Group by time-control category
            tc_buckets = {}
            for e in entries:
                key = e.get("tc_label", "Blitz")
                tc_buckets.setdefault(key, []).append(e)
            time_controls = []
            for tc_meta in _TC_CATEGORIES:
                bucket = tc_buckets.get(tc_meta["label"], [])
                if bucket:
                    stats = _compute_stats(bucket)
                    stats["label"] = tc_meta["label"]
                    stats["icon"] = tc_meta["icon"]
                    stats["css"] = tc_meta["css"]
                    time_controls.append(stats)
            return {
                "label": label,
                "icon": icon,
                "overall": overall,
                "time_controls": time_controls,
            }

        # Split entries by game type
        chess_tournament = [e for e in tournament_entries if e.get("game_type", "chess") == "chess"]
        chess_casual     = [e for e in oneonone_entries if e.get("game_type", "chess") == "chess"]
        bt_tournament    = [e for e in tournament_entries if e.get("game_type") == "breakthrough"]
        bt_casual        = [e for e in oneonone_entries if e.get("game_type") == "breakthrough"]

        chess_all = chess_tournament + chess_casual
        chess_all.sort(key=lambda h: h["date"], reverse=True)
        bt_all = bt_tournament + bt_casual
        bt_all.sort(key=lambda h: h["date"], reverse=True)

        ctx["game_type_stats"] = [
            {
                "game_type": "chess",
                "label": "Chess",
                "icon": "♟",
                "elo": user.elo_chess,
                "elo_css": "text-brand",
                "overall": _compute_stats(chess_all),
                "formats": [
                    _build_format_block("Tournament Games", "🏆", chess_tournament),
                    _build_format_block("One-on-One Games", "⚔️", chess_casual),
                ],
            },
            {
                "game_type": "breakthrough",
                "label": "Breakthrough",
                "icon": "♙",
                "elo": user.elo_breakthrough,
                "elo_css": "text-purple-400",
                "overall": _compute_stats(bt_all),
                "formats": [
                    _build_format_block("Tournament Games", "🏆", bt_tournament),
                    _build_format_block("One-on-One Games", "⚔️", bt_casual),
                ],
            },
        ]

        # Keep legacy format_stats for backward compat if anything else uses it
        ctx["format_stats"] = [
            _build_format_block("Tournament Games", "🏆", tournament_entries),
            _build_format_block("One-on-One Games", "⚔️", oneonone_entries),
        ]

        # \u2500\u2500 AI Models tab context \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        game_configs = []
        for g in GAME_TYPES:
            gm = user.get_game_model(g["type"])
            entry = {
                "type": g["type"],
                "label": g["label"],
                "model": gm,
                # model_files_status is loaded lazily via AJAX (users:model_file_status_api)
                # to avoid blocking the profile page load with HuggingFace HTTP requests.
                "model_files_status": None,
            }
            game_configs.append(entry)
        ctx["game_configs"] = game_configs

        # ── Tournament readiness per game type ─────────────
        from apps.users.integrity import TOURNAMENT_MIN_RATED_GAMES
        from apps.tournaments.models import TournamentParticipant
        from django.db.models import Q

        # Pre-fetch game types where this user has been DQ'd for repo
        # change in any tournament. Stored as a set of game_type strings
        # so the per-game-type loop below only does one query total.
        dq_game_types = set(
            TournamentParticipant.objects
            .filter(user=user, disqualified_for_sha_mismatch=True)
            .values_list("tournament__game_type", flat=True)
            .distinct()
        )

        tournament_readiness = []
        for g in GAME_TYPES:
            gtype = g["type"]
            gm = user.get_game_model(gtype)

            # Count rated (non-tournament) games for this game type
            rated_count = (
                Game.objects
                .filter(game_type=gtype)
                .filter(Q(white=user) | Q(black=user))
                .exclude(result__in=(Game.Result.NONE, ""))
                .exclude(is_tournament_game=True)
                .count()
            )
            games_needed_overall = max(0, TOURNAMENT_MIN_RATED_GAMES - rated_count)

            # Repo-change cooldown: games since last revalidation
            games_since_change = gm.rated_games_since_revalidation if gm else 0
            games_needed_after_change = max(0, TOURNAMENT_MIN_RATED_GAMES - games_since_change)

            # User changed repo (or was DQ'd from a tournament) and
            # hasn't played 30 games since. Four signals for this:
            # 1. Normal repo-change path: had 30+ total games, counter reset.
            # 2. Integrity flag flipped to False by DQ / SHA change detection.
            # 3. DQ history exists for this game type AND counter < 30
            #    (covers the case where re-validation already cleared the
            #    integrity flag back to True but cooldown hasn't been served).
            # 4. User has more total rated games than games since last
            #    revalidation — meaning the counter was reset at some point
            #    (repo changed while user had < 30 total games).
            repo_changed = (
                gm is not None
                and (
                    (rated_count >= TOURNAMENT_MIN_RATED_GAMES
                     and games_since_change < TOURNAMENT_MIN_RATED_GAMES)
                    or not gm.model_integrity_ok
                    or (gtype in dq_game_types
                        and games_since_change < TOURNAMENT_MIN_RATED_GAMES)
                    or (rated_count > games_since_change
                        and games_since_change < TOURNAMENT_MIN_RATED_GAMES)
                )
            )

            tournament_readiness.append({
                "game_type": gtype,
                "label": g["label"],
                "rated_count": rated_count,
                "games_needed_overall": games_needed_overall,
                "games_since_change": games_since_change,
                "games_needed_after_change": games_needed_after_change,
                "repo_changed": repo_changed,
                "ready": (
                    rated_count >= TOURNAMENT_MIN_RATED_GAMES
                    and (not repo_changed)
                    and gm is not None
                    and gm.is_verified
                    and gm.model_integrity_ok
                ),
                "TOURNAMENT_MIN_RATED_GAMES": TOURNAMENT_MIN_RATED_GAMES,
            })
        ctx["tournament_readiness"] = tournament_readiness

        return ctx

    def post(self, request, *args, **kwargs):
        """Handle both profile-edit and AI-model form submissions."""
        if request.POST.get("ai_model_form"):
            return self._handle_ai_model_post(request)
        return super().post(request, *args, **kwargs)

    def _handle_ai_model_post(self, request):
        import re as _re
        from .ownership_verification import (
            VERIFY_FILENAME,
            check_full_ownership,
            generate_verification_code,
        )

        action = request.POST.get("action", "connect")
        game_type = request.POST.get("game_type", "").strip()
        repo_id = request.POST.get("hf_model_repo_id", "").strip()
        data_repo_id = request.POST.get("hf_data_repo_id", "").strip()
        hf_space_url = request.POST.get("hf_space_url", "").strip().rstrip("/")

        if game_type not in [g["type"] for g in GAME_TYPES]:
            messages.error(request, "Invalid game type.")
            return redirect("users:profile")

        # ── Verify ownership action ────────────────────────────
        if action == "verify":
            submitted_repo_id = request.POST.get("hf_model_repo_id", "").strip()
            gm = request.user.get_game_model(game_type)
            if not gm:
                messages.error(request, "No model registered for this game type yet.")
            elif submitted_repo_id and submitted_repo_id != gm.hf_model_repo_id:
                # Cross-game protection: reject if the submitted repo_id doesn't match
                # the one stored for this game_type. Each game has its own verification
                # code on its own UserGameModel row (unique_together = user + game_type),
                # so a code issued for game A cannot legitimately verify game B.
                messages.error(
                    request,
                    f"Repository mismatch: the submitted repo '{submitted_repo_id}' does not "
                    f"match the registered {game_type} repo '{gm.hf_model_repo_id}'. "
                    "Please reconnect the correct repo first.",
                )
            else:
                ok, msg = check_full_ownership(gm)
                if ok:
                    messages.success(request, msg)
                else:
                    messages.error(request, msg)
            return redirect("users:profile")

        # ── Connect / update a repo ────────────────────────────
        if not repo_id:
            messages.error(request, "Please enter a Hugging Face repo ID.")
        elif not _re.match(r'^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$', repo_id):
            messages.error(request, "Invalid repo ID format (e.g. 'YourName/MyModel').")
        elif data_repo_id and not _re.match(r'^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$', data_repo_id):
            messages.error(request, "Invalid data repo ID format (e.g. 'YourName/breakthrough-data' or 'YourName/chess-data').")
        elif hf_space_url and not _re.match(r'^(?:https?://)?[a-zA-Z0-9._-]+\.hf\.space$', hf_space_url):
            messages.error(request, "HF Space URL must end in .hf.space (e.g. owner-myspace.hf.space).")
        elif (_gated_err := _check_repo_is_gated(repo_id)) is not None:
            messages.error(request, _gated_err)
        else:
            dup = UserGameModel.objects.filter(
                hf_model_repo_id=repo_id,
            ).exclude(user=request.user)
            if dup.exists():
                messages.error(
                    request,
                    "This model repository is already registered by another user.",
                )
            else:
                gm, created = UserGameModel.objects.get_or_create(
                    user=request.user,
                    game_type=game_type,
                    defaults={"hf_model_repo_id": repo_id, "hf_data_repo_id": data_repo_id},
                )
                if not created and (gm.hf_model_repo_id != repo_id or gm.hf_data_repo_id != data_repo_id):
                    gm.hf_model_repo_id = repo_id
                    gm.hf_data_repo_id = data_repo_id
                    gm.is_verified = False
                    gm.verification_code = ""
                    gm.save(update_fields=[
                        "hf_model_repo_id", "hf_data_repo_id",
                        "is_verified", "verification_code",
                    ])

                # ── HF Space URL: save with pending status ─────────────
                if hf_space_url:
                    # Normalise: ensure the stored value always has https://
                    if not hf_space_url.startswith(('http://', 'https://')):
                        hf_space_url = 'https://' + hf_space_url
                    UserGameModel.objects.filter(pk=gm.pk).update(
                        hf_inference_endpoint_url=hf_space_url,
                        hf_inference_endpoint_status="pending",
                    )
                    gm.hf_inference_endpoint_url = hf_space_url
                    gm.hf_inference_endpoint_status = "pending"

                label = dict((g["type"], g["label"]) for g in GAME_TYPES).get(game_type, game_type)
                if gm.is_verified:
                    # Repo unchanged and already verified — just confirm, no code reset
                    messages.success(request, f"{label} repo saved (already verified).")
                else:
                    # New repo or repo changed — issue a challenge code
                    code = generate_verification_code(gm)
                    space_note = (
                        f" Also add '{VERIFY_FILENAME}' to the root of your HF Space repo with the same code."
                        if hf_space_url else ""
                    )
                    data_note = (
                        f" And add '{VERIFY_FILENAME}' to the root of your data repo ({data_repo_id}) with the same code."
                        if data_repo_id else ""
                    )
                    messages.info(
                        request,
                        f"{label} repo saved. To prove ownership, create '{VERIFY_FILENAME}' "
                        f"at the root of {repo_id} containing exactly: {code}.{space_note}{data_note} "
                        "Then click 'Verify Ownership'.",
                    )

        return redirect("users:profile")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GDPR — Data Access & Deletion
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@login_required
def gdpr_portal(request):
    """GDPR portal: submit data-access or deletion requests, view history."""
    user = request.user
    existing = user.gdpr_requests.all()[:10]

    if request.method == "POST":
        form = GDPRRequestForm(request.POST)
        if form.is_valid():
            # Prevent duplicate pending requests of the same type
            pending = existing.filter(
                request_type=form.cleaned_data["request_type"],
                status__in=["pending", "processing"],
            )
            if pending.exists():
                messages.warning(
                    request,
                    "You already have a pending request of this type.",
                )
            else:
                obj = form.save(commit=False)
                obj.user = user
                obj.save()
                messages.success(
                    request,
                    "Your request has been submitted. We'll process it within 30 days.",
                )
            return redirect("users:gdpr")
    else:
        form = GDPRRequestForm()

    return render(request, "users/gdpr.html", {
        "form": form,
        "requests": existing,
    })


@login_required
def gdpr_export(request):
    """Return user's full data as JSON (GDPR Art. 15 — Right of Access)."""
    user = request.user
    data = {
        "username": user.username,
        "date_joined": user.date_joined.isoformat(),
        "last_login": user.last_login.isoformat() if user.last_login else None,
        "ai_name": user.ai_name,
        "elo": user.elo,
        "wins": user.wins,
        "losses": user.losses,
        "draws": user.draws,
        "total_games": user.total_games,
        "current_streak": user.current_streak,
        "tournaments_entered": list(
            user.tournaments.values_list("name", flat=True)
        ),
        "tournament_matches": list(
            user.tournament_matches_as_p1.values(
                "tournament__name", "round_num", "result",
                "elo_change_p1", "timestamp",
            )
        ) + list(
            user.tournament_matches_as_p2.values(
                "tournament__name", "round_num", "result",
                "elo_change_p2", "timestamp",
            )
        ),
        "casual_games_as_white": list(
            user.games_as_white.values(
                "result", "elo_change_white", "time_control", "timestamp",
            )
        ),
        "casual_games_as_black": list(
            user.games_as_black.values(
                "result", "elo_change_black", "time_control", "timestamp",
            )
        ),
    }
    response = JsonResponse(data, json_dumps_params={"indent": 2, "default": str})
    response["Content-Disposition"] = f'attachment; filename="{user.username}_data_export.json"'
    return response


@login_required
def gdpr_delete(request):
    """Process account deletion (GDPR Art. 17 — Right to Erasure)."""
    if request.method != "POST":
        return redirect("users:gdpr")

    user = request.user
    username = user.username
    user_id = user.pk

    # Remove the user's model folder (user_{id}) before deleting the DB record.
    user_folder = Path(settings.USER_MODELS_BASE_DIR) / f"user_{user_id}"
    if user_folder.exists():
        try:
            shutil.rmtree(user_folder)
            log.info("GDPR deletion: removed model folder %s", user_folder)
        except Exception:
            log.exception("GDPR deletion: failed to remove model folder %s", user_folder)

    # Permanently delete user — cascades to games, matches, etc.
    user.delete()

    messages.success(request, "Your account and all associated data have been permanently deleted.")
    log.info("GDPR deletion completed: user=%s", username)
    return redirect("core:home")


@login_required
def match_moves(request, match_id):
    """Return the UCI move list + FEN sequence for a completed tournament match.

    Used by the profile game-history replay board (AJAX).
    """
    import chess as _chess

    match = get_object_or_404(
        Match.objects.select_related("player1", "player2"),
        pk=match_id,
        match_status=Match.MatchStatus.COMPLETED,
    )

    # Get the primary (non-armageddon) game linked to this match
    from apps.games.models import Game
    game = match.games.filter(armageddon_of__isnull=True).first()
    if game is None:
        game = match.games.first()
    if game is None:
        return JsonResponse({"moves": [], "fens": []})

    # Build FEN list by replaying UCI moves
    board = _chess.Board()
    fens = [board.fen()]
    sans = []
    for uci in (game.move_list or []):
        try:
            move = _chess.Move.from_uci(uci)
            sans.append(board.san(move))
            board.push(move)
            fens.append(board.fen())
        except (ValueError, _chess.InvalidMoveError):
            break

    return JsonResponse({
        "moves": sans,
        "fens": fens,
        "result": game.result,
        "white": match.player1.username,
        "black": match.player2.username,
        "user_color": "white" if match.player1_id == request.user.pk else "black",
        "date": match.timestamp.strftime("%Y-%m-%d %H:%M") if match.timestamp else "",
        "game_type": "chess",
    })


@login_required
def model_file_status_api(request, game_type):
    """Return model + data repo file status as JSON (lazy-loaded by profile page).

    Called by the AI Models tab via AJAX to avoid blocking the profile page load
    with synchronous outbound HuggingFace HTTP requests.
    """
    if game_type not in [g["type"] for g in GAME_TYPES]:
        return JsonResponse({"error": "Invalid game type."}, status=400)
    gm = request.user.get_game_model(game_type)
    if not gm or not gm.hf_model_repo_id:
        return JsonResponse({"status": "no_model"})
    result = _build_breakthrough_file_status(request.user, gm)
    if result is None:
        return JsonResponse({"status": "no_data"})
    return JsonResponse({"status": "ok", **result})


@login_required
def activity_heatmap(request):
    """Return per-day game counts + game details for the last 365 days.

    Used by the profile activity-calendar heatmap (AJAX).
    Returns JSON: {days: [{date, count, games: [...]}], max_count, start_date}
    """
    from collections import defaultdict
    from datetime import timedelta
    import datetime

    user = request.user
    today = timezone.now().date()
    start = max(today - timedelta(days=364), user.date_joined.date())

    # ── Gather tournament matches ──────────────────
    t_as_p1 = Match.objects.filter(
        player1=user,
        match_status=Match.MatchStatus.COMPLETED,
        is_armageddon=False,
        timestamp__date__gte=start,
    ).select_related("player2", "tournament")
    t_as_p2 = Match.objects.filter(
        player2=user,
        match_status=Match.MatchStatus.COMPLETED,
        is_armageddon=False,
        timestamp__date__gte=start,
    ).select_related("player1", "tournament")

    COMPLETED = [
        Game.Status.WHITE_WINS, Game.Status.BLACK_WINS, Game.Status.DRAW,
        Game.Status.TIMEOUT_WHITE, Game.Status.TIMEOUT_BLACK,
    ]
    c_as_w = Game.objects.filter(
        white=user, is_tournament_game=False,
        status__in=COMPLETED, timestamp__date__gte=start,
    ).select_related("black")
    c_as_b = Game.objects.filter(
        black=user, is_tournament_game=False,
        status__in=COMPLETED, timestamp__date__gte=start,
    ).select_related("white")

    # bucket by date
    buckets = defaultdict(list)  # date_str -> [game_dict, ...]

    game_type_filter = request.GET.get("game_type")  # optional: 'chess' or 'breakthrough'

    for m in t_as_p1:
        gtype = getattr(m.tournament, "game_type", "chess") or "chess"
        if game_type_filter and gtype != game_type_filter:
            continue
        d = m.timestamp.date().isoformat()
        tc_cat = _tc_category(m.time_control)
        buckets[d].append({
            "id": m.pk, "type": "match",
            "opponent": m.player2.username,
            "opponent_flag": m.player2.country_flag,
            "result": m.result,
            "user_won": m.winner_id == user.pk,
            "is_draw": m.result == "1/2-1/2",
            "tc": m.time_control, "tc_css": tc_cat["css"],
            "tournament": m.tournament.name, "round": m.round_num,
            "time": m.timestamp.strftime("%H:%M"),
            "played_as": "white",
            "game_type": gtype,
        })
    for m in t_as_p2:
        gtype = getattr(m.tournament, "game_type", "chess") or "chess"
        if game_type_filter and gtype != game_type_filter:
            continue
        d = m.timestamp.date().isoformat()
        tc_cat = _tc_category(m.time_control)
        buckets[d].append({
            "id": m.pk, "type": "match",
            "opponent": m.player1.username,
            "opponent_flag": m.player1.country_flag,
            "result": m.result,
            "user_won": m.winner_id == user.pk,
            "is_draw": m.result == "1/2-1/2",
            "tc": m.time_control, "tc_css": tc_cat["css"],
            "tournament": m.tournament.name, "round": m.round_num,
            "time": m.timestamp.strftime("%H:%M"),
            "played_as": "black",
            "game_type": gtype,
        })
    for g in c_as_w:
        if game_type_filter and g.game_type != game_type_filter:
            continue
        d = g.timestamp.date().isoformat()
        tc_cat = _tc_category(g.time_control)
        buckets[d].append({
            "id": g.pk, "type": "game",
            "opponent": g.black.username if g.black else "?",
            "opponent_flag": g.black.country_flag if g.black else "",
            "result": g.result,
            "user_won": g.winner_id == user.pk,
            "is_draw": g.result == "1/2-1/2",
            "tc": g.time_control, "tc_css": tc_cat["css"],
            "tournament": None, "round": None,
            "time": g.timestamp.strftime("%H:%M"),
            "played_as": "white",
            "game_type": g.game_type,
        })
    for g in c_as_b:
        if game_type_filter and g.game_type != game_type_filter:
            continue
        d = g.timestamp.date().isoformat()
        tc_cat = _tc_category(g.time_control)
        buckets[d].append({
            "id": g.pk, "type": "game",
            "opponent": g.white.username if g.white else "?",
            "opponent_flag": g.white.country_flag if g.white else "",
            "result": g.result,
            "user_won": g.winner_id == user.pk,
            "is_draw": g.result == "1/2-1/2",
            "tc": g.time_control, "tc_css": tc_cat["css"],
            "tournament": None, "round": None,
            "time": g.timestamp.strftime("%H:%M"),
            "played_as": "black",
            "game_type": g.game_type,
        })

    # Build full day list (fill in zeros)
    days = []
    max_count = 0
    current = start
    while current <= today:
        ds = current.isoformat()
        games = buckets.get(ds, [])
        cnt = len(games)
        if cnt > max_count:
            max_count = cnt
        days.append({"date": ds, "count": cnt, "games": games})
        current += timedelta(days=1)

    return JsonResponse({
        "days": days,
        "max_count": max_count,
        "start_date": start.isoformat(),
    })


@login_required
def game_moves(request, game_id):
    """Return UCI move list + FEN sequence for a completed casual game (AJAX).

    Used by the profile game-history replay board for one-on-one games.
    """
    COMPLETED = [
        Game.Status.WHITE_WINS,
        Game.Status.BLACK_WINS,
        Game.Status.DRAW,
        Game.Status.TIMEOUT_WHITE,
        Game.Status.TIMEOUT_BLACK,
    ]
    game = get_object_or_404(Game, pk=game_id, status__in=COMPLETED)

    is_bt = game.game_type == Game.GameType.BREAKTHROUGH

    if is_bt:
        from apps.games.breakthrough_engine import (
            _fen_to_grid, _grid_to_fen, STARTING_FEN, WHITE, BLACK,
            WHITE_PIECE, BLACK_PIECE, EMPTY, BOARD_SIZE, _sq_to_coords,
            _opponent_turn,
        )
        grid, turn = _fen_to_grid(STARTING_FEN)
        fens = [STARTING_FEN]
        move_labels = []
        for uci in (game.move_list or []):
            move_labels.append(uci)
            fr, fc = _sq_to_coords(uci[:2])
            tr, tc = _sq_to_coords(uci[2:4])
            grid[tr][tc] = grid[fr][fc]
            grid[fr][fc] = EMPTY
            turn = _opponent_turn(turn)
            fens.append(_grid_to_fen(grid, turn))
    else:
        import chess as _chess
        board = _chess.Board()
        fens = [board.fen()]
        move_labels = []
        for uci in (game.move_list or []):
            try:
                move = _chess.Move.from_uci(uci)
                move_labels.append(board.san(move))
                board.push(move)
                fens.append(board.fen())
            except (ValueError, _chess.InvalidMoveError):
                break

    return JsonResponse({
        "moves": move_labels,
        "fens": fens,
        "result": game.result,
        "white": game.white.username if game.white else "?",
        "black": game.black.username if game.black else "?",
        "user_color": "white" if game.white_id == request.user.pk else "black",
        "date": game.timestamp.strftime("%Y-%m-%d %H:%M") if game.timestamp else "",
        "game_type": game.game_type,
    })

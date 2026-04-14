import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView, LogoutView
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views.generic import CreateView, UpdateView

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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Registration (CreateView) + IP cheating check
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _get_client_ip(request):
    """Extract the real client IP from the request."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


class RegisterView(CreateView):
    model = CustomUser
    form_class = RegistrationForm
    template_name = "users/register.html"
    success_url = reverse_lazy("users:activation_sent")

    def form_valid(self, form):
        user = form.save(commit=False)
        user.is_active = False  # Require email verification before activation
        user.save()

        # Record IP for multi-account detection
        ip = _get_client_ip(self.request)
        if ip:
            _check_duplicate_ip(ip, user)
        # Send activation email with a secure single-use token
        _send_activation_email(self.request, user)
        return redirect(self.success_url)


def _send_activation_email(request, user):
    """Send a single-use HMAC activation link to the user's email.

    Sends both plain-text and HTML versions using the Django template engine.
    """
    from django.template.loader import render_to_string
    from django.core.mail import EmailMultiAlternatives

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = account_activation_token.make_token(user)
    protocol = "https" if request.is_secure() else "http"
    domain = request.get_host()
    link = f"{protocol}://{domain}/users/activate/{uid}/{token}/"

    context = {
        "user": user,
        "activation_link": link,
    }
    subject = "Activate your Artificial Gladiator account"
    text_body = render_to_string("users/emails/activation_email.txt", context)
    html_body = render_to_string("users/emails/activation_email.html", context)

    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        to=[user.email],
    )
    email.attach_alternative(html_body, "text/html")
    email.send()


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
            login(request, user)
            messages.success(request, "Your account has been activated! Welcome.")
            return redirect("games:lobby")

    messages.error(
        request,
        "This activation link is invalid or has already been used.",
    )
    return redirect("users:login")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Daily model-integrity verification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@login_required
def verify_model(request):
    """Daily HF token prompt for model-integrity check.

    GET  → show the token form.
    POST → validate the token and check SHA against the original.
    """
    from .integrity import (
        validate_model_integrity, validate_all_models,
        needs_daily_validation, check_model_card,
    )

    # Already validated today — skip straight to the lobby.
    if not needs_daily_validation(request.user):
        return redirect("games:lobby")

    error = None
    model_card_warning = None
    if request.method == "POST":
        hf_token = request.POST.get("hf_token", "").strip()
        if not hf_token:
            error = "Please paste your Hugging Face read token."
        else:
            ok = validate_all_models(request.user, hf_token)
            if ok:
                # Check model cards (soft warning — does not block)
                from apps.users.models import UserGameModel
                for gm in UserGameModel.objects.filter(user=request.user, hf_model_repo_id__gt=""):
                    log.info("[verify-model] Integrity OK for user=%s, repo=%s. Running model card check...",
                             request.user.username, gm.hf_model_repo_id)
                    card_result = check_model_card(gm.hf_model_repo_id, hf_token)
                    log.info("[verify-model] Model card result: has_card=%s, missing=%s, message=%.120s",
                             card_result["has_card"], card_result["missing"], card_result["message"])
                    if not card_result["has_card"] or card_result["missing"]:
                        messages.warning(request, f"[{gm.game_type}] {card_result['message']}")
                messages.success(request, "All models verified successfully. You're cleared for today.")
                return redirect("games:lobby")
            else:
                # Distinguish "model changed" from "couldn't reach HF"
                from apps.users.models import UserGameModel
                compromised = UserGameModel.objects.filter(
                    user=request.user,
                    hf_model_repo_id__gt="",
                    model_integrity_ok=False,
                    last_model_validation_date=timezone.now().date(),
                ).exists()
                if compromised:
                    messages.warning(
                        request,
                        "⚠️ A change was detected in your AI model repository. "
                        "Tournament entry is blocked until you submit the new "
                        "revision for approval. Casual and rated games are "
                        "still available.",
                    )
                    return redirect("games:lobby")
                error = (
                    "One or more models could not be verified. "
                    "Please check that your token is valid and has read access to all your repos."
                )

    return render(request, "users/verify_model.html", {
        "error": error,
        "model_card_warning": model_card_warning,
    })


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
GAME_TYPES = [
    {"type": "chess", "label": "Chess"},
    {"type": "breakthrough", "label": "Breakthrough"},
]


@login_required
def ai_models(request):
    """Manage per-game AI model configurations.

    GET  → show current models with status for each game.
    POST → connect or re-verify a model for a specific game.
    """
    import re
    from .forms import validate_gated_hf_repo
    from .integrity import record_original_sha, check_model_card

    if request.method == "POST":
        game_type = request.POST.get("game_type", "").strip()
        repo_id = request.POST.get("hf_model_repo_id", "").strip()
        data_repo_id = request.POST.get("hf_data_repo_id", "").strip()
        hf_token = request.POST.get("hf_token", "").strip()

        if game_type not in [g["type"] for g in GAME_TYPES]:
            messages.error(request, "Invalid game type.")
        elif not repo_id:
            messages.error(request, "Please enter a Hugging Face repo ID.")
        elif not re.match(r'^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$', repo_id):
            messages.error(request, "Invalid repo ID format (e.g. 'YourName/MyModel').")
        elif data_repo_id and not re.match(r'^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$', data_repo_id):
            messages.error(request, "Invalid data repo ID format (e.g. 'YourName/breakthrough-data').")
        elif not hf_token:
            messages.error(request, "Please enter your Hugging Face access token.")
        else:
            try:
                validate_gated_hf_repo(repo_id, hf_token)
            except Exception as exc:
                # Extract validation error messages
                if hasattr(exc, "message_dict"):
                    for field_errors in exc.message_dict.values():
                        for err in field_errors:
                            messages.error(request, err)
                elif hasattr(exc, "messages"):
                    for err in exc.messages:
                        messages.error(request, err)
                else:
                    messages.error(request, str(exc))
            else:
                # ── Repo visibility & platform access checks ──
                _visibility_ok = True
                try:
                    from huggingface_hub import HfApi
                    from huggingface_hub.utils import GatedRepoError

                    user_api = HfApi(token=hf_token)
                    info = user_api.model_info(repo_id, token=hf_token)

                    if getattr(info, "private", False):
                        messages.error(
                            request,
                            "Your repo must be set to Gated, not Private. "
                            "Gated allows our platform to access it while "
                            "keeping it protected from the public. Go to "
                            "your repo Settings on HuggingFace → Danger Zone "
                            "→ Control who can access this repository → "
                            "Select Gated.",
                        )
                        _visibility_ok = False
                    elif not getattr(info, "gated", False):
                        messages.error(
                            request,
                            "Your repo must be set to Gated on HuggingFace, "
                            "not fully public. Go to your repo Settings → "
                            "Danger Zone → Control who can access this "
                            "repository → Select Gated.",
                        )
                        _visibility_ok = False
                except GatedRepoError:
                    # User token can't even access — should not happen since
                    # validate_gated_hf_repo already verified it.
                    messages.error(request, "Could not verify repo visibility.")
                    _visibility_ok = False
                except Exception as vis_exc:
                    log.warning("Visibility check failed for %s: %s", repo_id, vis_exc)
                    messages.error(request, "Could not verify repo visibility.")
                    _visibility_ok = False

                # Verify the platform account can access the gated repo
                if _visibility_ok:
                    try:
                        platform_api = HfApi(token=settings.HF_PLATFORM_TOKEN)
                        platform_api.model_info(repo_id, token=settings.HF_PLATFORM_TOKEN)
                    except (GatedRepoError, Exception) as plat_exc:
                        if "403" in str(plat_exc) or isinstance(plat_exc, GatedRepoError):
                            messages.error(
                                request,
                                f"Our platform does not have access to your "
                                f"gated repo yet. Please go to "
                                f"https://huggingface.co/{repo_id} and add "
                                f"'ArtificialGladiatorLeague' to your authorized users "
                                f"list, then try again.",
                            )
                            _visibility_ok = False
                        else:
                            log.warning("Platform access check failed for %s: %s", repo_id, plat_exc)
                            messages.error(request, "Could not verify platform access to your repo.")
                            _visibility_ok = False

                if not _visibility_ok:
                    return redirect("users:ai_models")

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
                    if not created:
                        gm.hf_model_repo_id = repo_id
                        gm.hf_data_repo_id = data_repo_id
                        gm.save(update_fields=["hf_model_repo_id", "hf_data_repo_id"])

                    record_original_sha(gm, hf_token)

                    # Run sandbox verification (download, scan, test)
                    try:
                        from apps.games.local_sandbox_inference import verify_model
                        verify_model(gm, token=hf_token)
                    except Exception:
                        log.exception(
                            "Sandbox verification failed for %s/%s",
                            request.user.username, game_type,
                        )
                        messages.warning(
                            request,
                            "Model connected but sandbox verification failed. "
                            "An admin will review it manually.",
                        )

                    # Ensure the model is cached for gameplay even if
                    # verify_model's persist section didn't run (e.g. it
                    # was already approved from a previous attempt).
                    try:
                        from apps.users.model_lifecycle import download_and_scan_for_user
                        download_and_scan_for_user(request.user.pk)
                    except Exception:
                        log.debug(
                            "Post-registration cache warm failed for %s/%s",
                            request.user.username, game_type, exc_info=True,
                        )

                    # Soft model-card check
                    card_result = check_model_card(repo_id, hf_token)
                    if not card_result["has_card"] or card_result["missing"]:
                        messages.warning(request, card_result["message"])

                    label = dict((g["type"], g["label"]) for g in GAME_TYPES).get(game_type, game_type)
                    messages.success(
                        request,
                        f"{label} model connected and verified successfully.",
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
        # Model download/verification is scheduled by the user_logged_in
        # signal in apps.users.signals. Keep the login response fast.
        return super().form_valid(form)


class UserLogoutView(LogoutView):
    next_page = "/"

    def dispatch(self, request, *args, **kwargs):
        # Logout cleanup is scheduled by the user_logged_out signal in
        # apps.users.signals which captures the user id and schedules
        # `cleanup_on_logout` asynchronously.
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

    # User's personal OAuth token (if they linked via HF OAuth).
    # For gated repos we try the user's token first, then fall back to
    # the platform token (settings.HF_PLATFORM_TOKEN) if configured.
    hf_token_user = get_user_hf_token(user)
    platform_token = settings.HF_PLATFORM_TOKEN or None
    hf_data_repo_id = gm.hf_data_repo_id or f"{user.username}/breakthrough-data"

    # Determine the correct revision for this user's model
    revision = gm.approved_full_sha or gm.submitted_ref or "main"

    # Model repo (gated — needs token)
    try:
        from huggingface_hub import HfApi, hf_hub_download
        from huggingface_hub.utils import (
            EntryNotFoundError,
            HfHubHTTPError,
            RepositoryNotFoundError,
            LocalEntryNotFoundError,
        )

        api = HfApi()
        # Try tokens in order: user's token, then platform token, then anonymous.
        tokens_to_try = []
        if hf_token_user:
            tokens_to_try.append(hf_token_user)
        if platform_token and platform_token not in tokens_to_try:
            tokens_to_try.append(platform_token)
        tokens_to_try.append(None)  # anonymous fallback

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
                model_files = [{"name": f, "present": True} for f in config["files"]]
                found_any = True

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
        from huggingface_hub import hf_hub_download
        from huggingface_hub.utils import (
            EntryNotFoundError,
            HfHubHTTPError,
            RepositoryNotFoundError,
        )

        config_path = hf_hub_download(
            repo_id=hf_data_repo_id,
            filename="config_data.json",
            repo_type="dataset",
            token=None,
        )
        with open(config_path, "r") as fh:
            config = _json.load(fh)
        data_files = [{"name": f, "present": True} for f in config["files"]]
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
                "model_files_status": None,
            }
            if g["type"] == "breakthrough" and gm and gm.hf_model_repo_id:
                entry["model_files_status"] = _build_breakthrough_file_status(
                    user, gm,
                )
            game_configs.append(entry)
        ctx["game_configs"] = game_configs

        return ctx

    def post(self, request, *args, **kwargs):
        """Handle both profile-edit and AI-model form submissions."""
        if request.POST.get("ai_model_form"):
            return self._handle_ai_model_post(request)
        return super().post(request, *args, **kwargs)

    def _handle_ai_model_post(self, request):
        import re as _re
        from .forms import validate_gated_hf_repo
        from .integrity import record_original_sha, check_model_card

        game_type = request.POST.get("game_type", "").strip()
        repo_id = request.POST.get("hf_model_repo_id", "").strip()
        data_repo_id = request.POST.get("hf_data_repo_id", "").strip()
        hf_token = request.POST.get("hf_token", "").strip()

        if game_type not in [g["type"] for g in GAME_TYPES]:
            messages.error(request, "Invalid game type.")
        elif not repo_id:
            messages.error(request, "Please enter a Hugging Face repo ID.")
        elif not _re.match(r'^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$', repo_id):
            messages.error(request, "Invalid repo ID format (e.g. 'YourName/MyModel').")
        elif data_repo_id and not _re.match(r'^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$', data_repo_id):
            messages.error(request, "Invalid data repo ID format (e.g. 'YourName/breakthrough-data').")
        elif not hf_token:
            messages.error(request, "Please enter your Hugging Face access token.")
        else:
            try:
                validate_gated_hf_repo(repo_id, hf_token)
            except Exception as exc:
                if hasattr(exc, "message_dict"):
                    for field_errors in exc.message_dict.values():
                        for err in field_errors:
                            messages.error(request, err)
                elif hasattr(exc, "messages"):
                    for err in exc.messages:
                        messages.error(request, err)
                else:
                    messages.error(request, str(exc))
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
                    if not created:
                        gm.hf_model_repo_id = repo_id
                        gm.hf_data_repo_id = data_repo_id
                        gm.save(update_fields=["hf_model_repo_id", "hf_data_repo_id"])

                    record_original_sha(gm, hf_token)

                    card_result = check_model_card(repo_id, hf_token)
                    if not card_result["has_card"] or card_result["missing"]:
                        messages.warning(request, card_result["message"])

                    label = dict((g["type"], g["label"]) for g in GAME_TYPES).get(game_type, game_type)
                    messages.success(
                        request,
                        f"{label} model connected and verified successfully.",
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
        "casual_games_as_p1": list(
            user.casual_games_as_p1.values(
                "result", "elo_change_p1", "time_control", "timestamp",
            )
        ),
        "casual_games_as_p2": list(
            user.casual_games_as_p2.values(
                "result", "elo_change_p2", "time_control", "timestamp",
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

    for m in t_as_p1:
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
        })
    for m in t_as_p2:
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
        })
    for g in c_as_w:
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

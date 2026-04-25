# ──────────────────────────────────────────────
# apps/users/webhooks.py
#
# Hugging Face webhook receiver & registration.
#
# HF sends a POST request to our endpoint whenever
# a subscribed repo changes.  We verify the
# signature, look up the affected user, and mark
# their model as "new revision available".
#
# Registration helper
# ────────────────────
# ``register_hf_webhook()`` uses the HF Hub API
# to create a webhook subscription for a user's
# repo so we get notified on push events.
#
# Security
# ────────
# • The webhook secret is stored in Django settings
#   (``HF_WEBHOOK_SECRET``) — never in source code.
# • Payloads are verified via HMAC-SHA256 before
#   any database writes.
# • The HF token used for registration is passed
#   as a runtime argument and is NEVER stored.
# ──────────────────────────────────────────────
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Webhook receiver endpoint
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _verify_signature(payload_body: bytes, signature: str, secret: str) -> bool:
    """Verify the HMAC-SHA256 signature sent by Hugging Face."""
    expected = hmac.new(
        secret.encode(), payload_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@csrf_exempt
@require_POST
def hf_webhook(request):
    """Receive repo-change notifications from Hugging Face.

    Expected headers:
        X-Webhook-Secret  — HMAC-SHA256 hex digest of the raw body

    Expected JSON payload (subset we use):
        {
          "event": { "action": "update", "scope": "repo.content" },
          "repo":  { "name": "user/model-name", "type": "model" }
        }
    """
    secret = getattr(settings, "HF_WEBHOOK_SECRET", "")
    if not secret:
        log.error("HF_WEBHOOK_SECRET is not configured — rejecting webhook")
        return HttpResponse(status=500)

    # ── Verify signature ─────────────────────────────────────
    signature = request.headers.get("X-Webhook-Secret", "")
    if not _verify_signature(request.body, signature, secret):
        log.warning("HF webhook signature mismatch — rejecting")
        return HttpResponse(status=403)

    # ── Parse payload ────────────────────────────────────────
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return HttpResponse("Invalid JSON", status=400)

    event = payload.get("event", {})
    repo = payload.get("repo", {})
    repo_name = repo.get("name", "")
    repo_type = repo.get("type", "model")
    action = event.get("action", "")
    scope = event.get("scope", "")

    log.info("HF webhook: repo=%s type=%s action=%s scope=%s",
             repo_name, repo_type, action, scope)

    # We only care about content updates (pushes)
    if action != "update" or scope != "repo.content":
        return JsonResponse({"status": "ignored", "reason": "not a content update"})

    if not repo_name:
        return HttpResponse("Missing repo name", status=400)

    # ── Handle Space deploys — update hf_inference_endpoint_url ─────────────
    if repo_type == "space":
        return _handle_space_update(repo_name)

    # ── Handle model repo changes — mark users for re-verification ──────────
    from apps.users.models import CustomUser

    affected = CustomUser.objects.filter(
        hf_model_repo_id=repo_name,
        model_integrity_ok=True,
    )
    count = affected.update(model_integrity_ok=False)

    log.info("HF webhook: marked %d user(s) for re-verification (repo=%s)",
             count, repo_name)

    return JsonResponse({
        "status": "ok",
        "users_flagged": count,
    })


def _handle_space_update(space_repo_name: str):
    """Re-probe the Space and update hf_inference_endpoint_url / status.

    Called when HF delivers a webhook for a *space* repo push.
    The Space owner is matched against UserGameModel records whose
    derived URL would point to this Space, then probed to confirm liveness.

    space_repo_name example: ``"john/chess-space"``
    Derived Space URL:        ``"https://john-chess-space.hf.space"``
    """
    import threading
    import requests as _req
    from apps.users.models import UserGameModel

    # Derive the public Space URL from the space repo slug
    owner, _, slug = space_repo_name.partition("/")
    space_url = f"https://{owner}-{slug}.hf.space" if slug else f"https://{owner}-{owner}.hf.space"

    # Find all UserGameModel rows that currently point at this Space URL
    # OR whose derived owner matches (catches newly-added records whose
    # URL was set by the populate_hf_space_url signal).
    affected_qs = UserGameModel.objects.filter(
        hf_inference_endpoint_url=space_url,
    )
    # Also catch records where the repo owner matches and URL is derived
    owner_derived_url = f"https://{owner}-{owner}.hf.space"
    if owner_derived_url != space_url:
        from django.db.models import Q
        affected_qs = UserGameModel.objects.filter(
            Q(hf_inference_endpoint_url=space_url)
            | Q(hf_inference_endpoint_url=owner_derived_url)
        )

    ugm_pks = list(affected_qs.values_list("pk", flat=True))
    log.info(
        "HF Space webhook: space=%s url=%s — found %d UGM record(s) to re-probe",
        space_repo_name, space_url, len(ugm_pks),
    )

    def _probe_and_update(pks: list, url: str) -> None:
        try:
            resp = _req.post(
                f"{url}/gradio_api/call/get_move",
                json={"data": ["rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"]},
                headers={"Content-Type": "application/json"},
                timeout=20,
            )
            new_status = "ready" if resp.status_code < 500 else "failed"
        except Exception as exc:
            log.warning("Space probe failed url=%s: %s", url, exc)
            new_status = "failed"

        if pks:
            UserGameModel.objects.filter(pk__in=pks).update(
                hf_inference_endpoint_url=url,
                hf_inference_endpoint_status=new_status,
            )
            log.info("Space probe: status=%s updated %d UGM record(s)", new_status, len(pks))

    threading.Thread(
        target=_probe_and_update,
        args=(ugm_pks, space_url),
        daemon=True,
        name=f"space-webhook-probe-{owner}",
    ).start()

    return JsonResponse({
        "status": "ok",
        "space": space_repo_name,
        "space_url": space_url,
        "ugm_records_queued": len(ugm_pks),
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Webhook registration helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def register_hf_webhook(
    repo_id: str,
    hf_token: str,
    webhook_url: str,
) -> dict | None:
    """Register a webhook with Hugging Face Hub for a specific repo.

    Uses the ``huggingface_hub`` SDK to create a webhook subscription
    that fires on repo content updates (pushes).

    Args:
        repo_id:     HF repo ID (e.g. ``"user/my-model"``).
        hf_token:    User Access Token with at least ``read`` scope.
                     Used once here and never stored.
        webhook_url: The absolute URL to our ``hf_webhook`` endpoint
                     (e.g. ``"https://agladiator.com/users/webhooks/hf/"``).

    Returns:
        A dict with the webhook details on success, or None on failure.
    """
    secret = getattr(settings, "HF_WEBHOOK_SECRET", "")
    if not secret:
        log.error("HF_WEBHOOK_SECRET is not configured — cannot register webhook")
        return None

    try:
        from huggingface_hub import create_webhook

        webhook = create_webhook(
            url=webhook_url,
            watched=[{"type": "model", "name": repo_id}],
            domains=["repo"],
            secret=secret,
            token=hf_token,
        )
        log.info("Registered HF webhook for %s: id=%s", repo_id, webhook.id)
        return {
            "id": webhook.id,
            "url": webhook.url,
            "watched": [{"type": "model", "name": repo_id}],
        }
    except Exception:
        log.exception("Failed to register HF webhook for %s", repo_id)
    return None

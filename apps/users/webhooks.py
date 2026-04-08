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

    # ── Mark affected users ──────────────────────────────────
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

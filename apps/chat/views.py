from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt


def notifications_list(request):
    """Return a lightweight notifications payload.

    This is a minimal placeholder that returns an empty list and zero
    unread count. Replace with a real Notification model lookup when
    restoring the full chat/notifications feature.
    """
    return JsonResponse({"unread_count": 0, "notifications": []})


@require_POST
@csrf_exempt
def notifications_mark_read(request):
    """Minimal endpoint to mark notifications read (no-op).

    Expects a POST with an `id` form field; currently treated as a no-op
    to keep the frontend functional while the full feature is absent.
    """
    return JsonResponse({"ok": True})

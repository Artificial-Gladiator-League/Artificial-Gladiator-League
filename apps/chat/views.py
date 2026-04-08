import json

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import Conversation, FriendRequest, Message, Notification

User = get_user_model()


def _broadcast_unread_count(user):
    """Push a fresh unread_count to the user's notification WebSocket."""
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        from .consumers import notif_group_name

        channel_layer = get_channel_layer()
        count = Notification.unread_count(user)
        async_to_sync(channel_layer.group_send)(
            notif_group_name(user.pk),
            {"type": "send_notification", "verb": "_refresh", "actor": "",
             "message": "", "url": "", "unread_count": count},
        )
    except Exception:
        pass  # WebSocket broadcast is best-effort


def _push_notif_sync(recipient, actor, verb, message_text, url):
    """Create a Notification row and push it to the user's WS group.
    Safe to call from synchronous Django views (uses async_to_sync)."""
    Notification.objects.create(recipient=recipient, actor=actor, verb=verb)
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        from .consumers import notif_group_name

        count = Notification.unread_count(recipient)
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            notif_group_name(recipient.pk),
            {
                "type": "send_notification",
                "verb": verb,
                "actor": actor.username,
                "message": message_text,
                "url": url,
                "unread_count": count,
            },
        )
    except Exception:
        pass  # WebSocket broadcast is best-effort


def _dismiss_friend_request_notification(recipient, sender):
    """Mark the friend-request notification as read and push updated count."""
    Notification.objects.filter(
        recipient=recipient,
        actor=sender,
        verb=Notification.Verb.FRIEND_REQUEST,
        is_read=False,
    ).update(is_read=True)
    _broadcast_unread_count(recipient)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Friend requests (AJAX endpoints)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@login_required
@require_POST
def send_friend_request(request):
    """Send a friend request to another user."""
    receiver_username = request.POST.get("username", "").strip()
    if not receiver_username:
        return JsonResponse({"error": "Username required."}, status=400)

    receiver = User.objects.filter(username=receiver_username).first()
    if receiver is None:
        return JsonResponse({"error": "User not found."}, status=404)

    if receiver == request.user:
        return JsonResponse({"error": "You cannot add yourself."}, status=400)

    # Already friends?
    if request.user.is_friend(receiver):
        return JsonResponse({"error": "Already friends."}, status=400)

    # Pending request already exists (either direction)?
    existing = FriendRequest.objects.filter(
        Q(sender=request.user, receiver=receiver)
        | Q(sender=receiver, receiver=request.user),
        status=FriendRequest.Status.PENDING,
    ).first()
    if existing:
        # If the other person already sent us a request, auto-accept
        if existing.sender == receiver:
            existing.status = FriendRequest.Status.ACCEPTED
            existing.save(update_fields=["status", "updated_at"])
            _dismiss_friend_request_notification(request.user, receiver)
            return JsonResponse({"status": "accepted", "message": f"You are now friends with {receiver.username}!"})
        return JsonResponse({"error": "Friend request already sent."}, status=400)

    FriendRequest.objects.create(sender=request.user, receiver=receiver)

    # Push real-time notification to the receiver  
    _push_notif_sync(
        recipient=receiver,
        actor=request.user,
        verb=Notification.Verb.FRIEND_REQUEST,
        message_text=f"{request.user.username} sent you a friend request",
        url=f"/users/profile/@{request.user.username}/",
    )

    return JsonResponse({"status": "sent", "message": f"Friend request sent to {receiver.username}."})


@login_required
@require_POST
def unfriend(request):
    """Remove an existing friendship (delete the accepted FriendRequest)."""
    username = request.POST.get("username", "").strip()
    other = User.objects.filter(username=username).first()
    if other is None or other == request.user:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"error": "Invalid user."}, status=400)
        return redirect("users:profile")

    # Delete accepted FriendRequests in both directions
    deleted, _ = FriendRequest.objects.filter(
        Q(sender=request.user, receiver=other) | Q(sender=other, receiver=request.user),
        status=FriendRequest.Status.ACCEPTED,
    ).delete()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"status": "unfriended" if deleted else "not_friends"})

    from django.contrib import messages as django_messages
    if deleted:
        django_messages.success(request, f"You are no longer friends with {other.username}.")
    else:
        django_messages.info(request, f"You were not friends with {other.username}.")
    return redirect("users:public_profile", username=other.username)


@login_required
@require_POST
def respond_friend_request(request, pk):
    """Accept or decline a friend request."""
    fr = get_object_or_404(FriendRequest, pk=pk, receiver=request.user, status=FriendRequest.Status.PENDING)
    action = request.POST.get("action", "").lower()
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    if action == "accept":
        fr.status = FriendRequest.Status.ACCEPTED
        fr.save(update_fields=["status", "updated_at"])
        _dismiss_friend_request_notification(request.user, fr.sender)
        if is_ajax:
            return JsonResponse({"status": "accepted", "message": f"You are now friends with {fr.sender.username}!"})
        from django.contrib import messages
        messages.success(request, f"You are now friends with {fr.sender.username}!")
        return redirect("users:public_profile", username=fr.sender.username)
    elif action == "decline":
        fr.status = FriendRequest.Status.REJECTED
        fr.save(update_fields=["status", "updated_at"])
        _dismiss_friend_request_notification(request.user, fr.sender)
        if is_ajax:
            return JsonResponse({"status": "declined"})
        return redirect("users:profile")
    else:
        if is_ajax:
            return JsonResponse({"error": "Invalid action. Use 'accept' or 'decline'."}, status=400)
        return redirect("users:profile")


@login_required
def friend_list(request):
    """Return JSON list of friends + pending requests (for sidebar/dropdown)."""
    user = request.user
    friends = user.friends.values_list("username", flat=True)
    pending_received = user.pending_received_requests.select_related("sender").values(
        "pk", "sender__username", "created_at",
    )
    pending_sent = user.pending_sent_requests.select_related("receiver").values(
        "pk", "receiver__username", "created_at",
    )
    return JsonResponse({
        "friends": list(friends),
        "pending_received": [
            {"id": p["pk"], "from": p["sender__username"], "date": p["created_at"].isoformat()}
            for p in pending_received
        ],
        "pending_sent": [
            {"id": p["pk"], "to": p["receiver__username"], "date": p["created_at"].isoformat()}
            for p in pending_sent
        ],
    })


@login_required
def friend_status(request, username):
    """Return the friendship status between the logged-in user and the given username."""
    other = User.objects.filter(username=username).first()
    if other is None or other == request.user:
        return JsonResponse({"status": "none"})

    if request.user.is_friend(other):
        return JsonResponse({"status": "friends"})

    pending = FriendRequest.objects.filter(
        Q(sender=request.user, receiver=other)
        | Q(sender=other, receiver=request.user),
        status=FriendRequest.Status.PENDING,
    ).first()
    if pending:
        if pending.sender == request.user:
            return JsonResponse({"status": "request_sent"})
        return JsonResponse({"status": "request_received", "request_id": pending.pk})

    return JsonResponse({"status": "none"})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Chat views
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@login_required
def chat_view(request, username):
    """Render the 1-on-1 chat page with a friend."""
    friend = get_object_or_404(User, username=username)

    # Security: only friends may chat
    if not request.user.is_friend(friend):
        return render(request, "chat/no_access.html", {"friend": friend}, status=403)

    conversation = Conversation.get_or_create_for_users(request.user, friend)

    # Mark unread messages from friend as read
    conversation.messages.filter(sender=friend, is_read=False).update(is_read=True)

    # Clear 'new_message' notifications from this friend + broadcast updated count
    Notification.objects.filter(
        recipient=request.user,
        actor=friend,
        verb=Notification.Verb.NEW_MESSAGE,
        is_read=False,
    ).update(is_read=True)
    _broadcast_unread_count(request.user)

    # Load last 50 messages for initial render
    recent_messages = conversation.messages.select_related("sender").order_by("-created_at")[:50]
    recent_messages = list(reversed(recent_messages))

    return render(request, "chat/chat.html", {
        "friend": friend,
        "conversation": conversation,
        "chat_messages": recent_messages,
    })


@login_required
def chat_history(request, username):
    """Return older messages (pagination) as JSON."""
    friend = get_object_or_404(User, username=username)
    if not request.user.is_friend(friend):
        return JsonResponse({"error": "Not friends."}, status=403)

    conversation = Conversation.get_or_create_for_users(request.user, friend)
    before_id = request.GET.get("before")
    qs = conversation.messages.select_related("sender").order_by("-created_at")
    if before_id:
        qs = qs.filter(pk__lt=int(before_id))
    older = list(qs[:30])
    older.reverse()

    return JsonResponse({
        "messages": [
            {
                "id": m.pk,
                "sender": m.sender.username,
                "text": m.text,
                "created_at": m.created_at.isoformat(),
                "is_read": m.is_read,
            }
            for m in older
        ],
        "has_more": qs.count() > 30,
    })


@login_required
def inbox(request):
    """Render the chat inbox — list of conversations."""
    user = request.user
    convos = Conversation.objects.filter(
        Q(user1=user) | Q(user2=user)
    ).select_related("user1", "user2").order_by("-created_at")

    conversations = []
    for c in convos:
        other = c.user2 if c.user1 == user else c.user1
        last_msg = c.messages.order_by("-created_at").first()
        unread = c.messages.filter(sender=other, is_read=False).count()
        conversations.append({
            "friend": other,
            "last_message": last_msg,
            "unread_count": unread,
        })

    # Sort by last message time (most recent first)
    conversations.sort(
        key=lambda x: x["last_message"].created_at if x["last_message"] else x["friend"].date_joined,
        reverse=True,
    )

    return render(request, "chat/inbox.html", {"conversations": conversations})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Notification endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@login_required
def notifications_list(request):
    """Return latest notifications as JSON (for the bell dropdown)."""
    notifs = Notification.recent(request.user, limit=10)
    return JsonResponse({
        "notifications": [
            {
                "id": n.pk,
                "actor": n.actor.username,
                "verb": n.verb,
                "is_read": n.is_read,
                "created_at": n.created_at.isoformat(),
                "url": _notif_url(n),
            }
            for n in notifs
        ],
        "unread_count": Notification.unread_count(request.user),
    })


@login_required
@require_POST
def notifications_read(request):
    """Mark all (or a single) notification as read."""
    notif_id = request.POST.get("id")
    qs = Notification.objects.filter(recipient=request.user, is_read=False)
    if notif_id:
        qs = qs.filter(pk=int(notif_id))
    qs.update(is_read=True)
    count = Notification.unread_count(request.user)
    _broadcast_unread_count(request.user)
    return JsonResponse({"status": "ok", "unread_count": count})


def _notif_url(notif):
    """Build a link URL for a notification."""
    if notif.verb == Notification.Verb.FRIEND_REQUEST:
        return f"/users/profile/@{notif.actor.username}/"
    if notif.verb == Notification.Verb.NEW_MESSAGE:
        return f"/chat/@{notif.actor.username}/"
    return ""

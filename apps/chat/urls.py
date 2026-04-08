from django.urls import path
from . import views

app_name = "chat"

urlpatterns = [
    # Friend system
    path("friends/", views.friend_list, name="friend_list"),
    path("friends/send/", views.send_friend_request, name="send_friend_request"),
    path("friends/respond/<int:pk>/", views.respond_friend_request, name="respond_friend_request"),
    path("friends/status/<str:username>/", views.friend_status, name="friend_status"),
    path("friends/unfriend/", views.unfriend, name="unfriend"),
    # Notifications
    path("notifications/", views.notifications_list, name="notifications_list"),
    path("notifications/read/", views.notifications_read, name="notifications_read"),
    # Chat
    path("inbox/", views.inbox, name="inbox"),
    path("@<str:username>/", views.chat_view, name="chat"),
    path("@<str:username>/history/", views.chat_history, name="chat_history"),
]

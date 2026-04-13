from django.urls import path
from . import views

urlpatterns = [
    path("notifications/", views.notifications_list, name="chat_notifications"),
    path("notifications/read/", views.notifications_mark_read, name="chat_notifications_read"),
]

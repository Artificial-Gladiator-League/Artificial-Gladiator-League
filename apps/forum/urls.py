from django.urls import path

from . import views

app_name = "forum"

urlpatterns = [
    path("", views.forum_home, name="home"),
    path("c/<slug:slug>/", views.category_detail, name="category"),
    path("c/<slug:slug>/new/", views.create_thread, name="create_thread"),
    path("t/<int:pk>/", views.thread_detail, name="thread"),
    path("t/<int:pk>/reply/", views.reply_to_thread, name="reply"),
]

from django.urls import path

from . import views

app_name = "forum"

urlpatterns = [
    path("", views.category_list, name="category_list"),
    path("c/<slug:category_slug>/", views.topic_list, name="topic_list"),
    path("c/<slug:category_slug>/new/", views.new_topic, name="new_topic"),
    path("c/<slug:category_slug>/t/<int:pk>/", views.topic_detail, name="topic_detail"),
    path("c/<slug:category_slug>/t/<int:pk>/reply/", views.add_reply, name="add_reply"),
    path("post/<int:pk>/like/", views.toggle_like, name="toggle_like"),
]

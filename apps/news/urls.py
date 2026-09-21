from django.urls import path

from . import views

app_name = "news"

urlpatterns = [
    path("", views.news_list, name="list"),
    path("new/", views.news_create, name="create"),
    path("<slug:slug>/", views.news_detail, name="detail"),
    path("<slug:slug>/edit/", views.news_edit, name="edit"),
    path("<slug:slug>/delete/", views.news_delete, name="delete"),
]

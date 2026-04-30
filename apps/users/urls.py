from django.urls import path
from . import views
from .webhooks import hf_webhook
from .hf_oauth import hf_oauth_start, hf_oauth_callback, hf_oauth_complete

app_name = "users"

urlpatterns = [
    path("webhooks/hf/", hf_webhook, name="hf_webhook"),
    # HF OAuth
    path("oauth/hf/", hf_oauth_start, name="hf_oauth_start"),
    path("oauth/hf/callback/", hf_oauth_callback, name="hf_oauth_callback"),
    path("oauth/hf/complete/", hf_oauth_complete, name="hf_oauth_complete"),
    path("register/", views.RegisterView.as_view(), name="register"),
    path("activate/<uidb64>/<token>/", views.activate, name="activate"),
    path("activation-sent/", views.activation_sent, name="activation_sent"),
    path("login/", views.UserLoginView.as_view(), name="login"),
    path("logout/", views.UserLogoutView.as_view(), name="logout"),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("profile/ai-models/", views.ai_models, name="ai_models"),
    path("profile/@<str:username>/", views.public_profile, name="public_profile"),
    path("match/<int:match_id>/moves/", views.match_moves, name="match_moves"),
    path("game/<int:game_id>/moves/", views.game_moves, name="game_moves"),
    path("activity-heatmap/", views.activity_heatmap, name="activity_heatmap"),
    path("search/", views.user_search, name="user_search"),
    # GDPR
    path("gdpr/", views.gdpr_portal, name="gdpr"),
    path("gdpr/export/", views.gdpr_export, name="gdpr_export"),
    path("gdpr/delete/", views.gdpr_delete, name="gdpr_delete"),
]

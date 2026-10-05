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
    path("register/", views.register, name="register"),
    path("activate/<uidb64>/<token>/", views.activate, name="activate"),
    path("activation-sent/", views.activation_sent, name="activation_sent"),
    path("login/", views.UserLoginView.as_view(), name="login"),
    path("logout/", views.UserLogoutView.as_view(), name="logout"),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("profile/@<str:username>/", views.public_profile, name="public_profile"),
    path("profile/gladiator/save/", views.save_gladiator_field, name="save_gladiator_field"),
    path("match/<int:match_id>/moves/", views.match_moves, name="match_moves"),
    path("game/<int:game_id>/moves/", views.game_moves, name="game_moves"),
    path("activity-heatmap/", views.activity_heatmap, name="activity_heatmap"),
    path("profile/model-file-status/<str:game_type>/", views.model_file_status_api, name="model_file_status_api"),
    path("search/", views.user_search, name="user_search"),
    # Diagnostics tab (own profile)
    path("profile/diagnostics/<str:game_type>/run/", views.run_diagnostics_start, name="run_diagnostics"),
    path("profile/diagnostics/run/<int:run_id>/status/", views.diagnostics_status, name="diagnostics_status"),
    path("profile/diagnostics/<str:game_type>/latest/", views.diagnostics_latest, name="diagnostics_latest"),
    # GDPR
    path("gdpr/", views.gdpr_portal, name="gdpr"),
    path("gdpr/export/", views.gdpr_export, name="gdpr_export"),
    path("gdpr/delete/", views.gdpr_delete, name="gdpr_delete"),
    # PayPal payout email
    path("profile/paypal-email/save/", views.save_paypal_email, name="save_paypal_email"),
    path("profile/paypal-email/delete/", views.delete_paypal_email, name="delete_paypal_email"),
    path("profile/country/", views.save_country, name="save_country"),
    # Email change
    path("profile/email/request-change/", views.request_email_change, name="request_email_change"),
    path("profile/email/confirm/<uidb64>/<token>/", views.confirm_email_change, name="confirm_email_change"),
    # Password change
    path("profile/password/change/", views.change_password, name="change_password"),
]

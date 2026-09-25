from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("two-factor/setup/", views.mfa_setup, name="mfa_setup"),
    path("two-factor/verify/", views.mfa_verify, name="mfa_verify"),
    path("two-factor/recovery-codes/", views.recovery_codes, name="recovery_codes"),
    path("two-factor/recovery-codes/new/", views.regenerate_recovery_codes, name="regenerate_recovery_codes"),
    path("profile/", views.profile, name="profile"),
    path("security/", views.security, name="security"),
    path("set-password/<uidb64>/<token>/", views.set_password, name="set_password"),
    path("users/", views.user_list, name="user_list"),
    path("users/new/", views.user_edit, name="user_create"),
    path("users/<int:pk>/", views.user_edit, name="user_edit"),
    path("users/<int:pk>/invite/", views.user_invite, name="user_invite"),
    path("users/<int:pk>/reset-2fa/", views.user_reset_mfa, name="user_reset_mfa"),
    path("users/<int:pk>/unlock/", views.user_unlock, name="user_unlock"),
]

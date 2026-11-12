from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("signup/", views.SignupView.as_view(), name="signup"),
    path("confirm/<str:token>/", views.confirm_email, name="confirm_email"),
    path("confirm/", views.resend_confirmation, name="resend_confirmation"),
    path("login/", views.TwoFactorLoginView.as_view(), name="login"),
    path("login/2fa/", views.two_factor_verify, name="two_factor_verify"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("account/security/", views.security, name="security"),
    path("account/2fa/setup/", views.TwoFactorSetupView.as_view(), name="two_factor_setup"),
    path("account/2fa/disable/", views.two_factor_disable, name="two_factor_disable"),
    path("password-reset/", auth_views.PasswordResetView.as_view(), name="password_reset"),
    path("password-reset/done/", auth_views.PasswordResetDoneView.as_view(), name="password_reset_done"),
    path(
        "password-reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path("password-reset/complete/", auth_views.PasswordResetCompleteView.as_view(), name="password_reset_complete"),
]

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth
from django.urls import path

from trading import views
from trading import profiles
from trading.bot_views import bot_settings
from trading.strategy_lab import strategy_comparison

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("sign-in/", auth.LoginView.as_view(
        template_name="trading/page.html",
        extra_context={"title": "Sign in", "submit": "Sign in"},
    ), name="login"),
    path("sign-out/", auth.LogoutView.as_view(), name="logout"),
    path("register/", views.register, name="register"),
    path("activate/<uuid:token>/", views.activate, name="activate"),
    path("accounts/new/", views.create_account, name="create_account"),
    path("accounts/<int:pk>/", views.account_page, name="account"),
    path("accounts/<int:pk>/deposit/", views.add_deposit, name="deposit"),
    path("accounts/<int:pk>/schedule/", views.toggle_schedule, name="schedule"),
    path("accounts/<int:pk>/run/", views.run, name="run"),
    path("accounts/<int:pk>/market/", views.market, name="market"),
    path("accounts/<int:pk>/market/new/", views.add_instrument, name="add_instrument"),
    path("accounts/<int:pk>/market/<int:item>/stops/", views.stops, name="stops"),
    path("accounts/<int:pk>/market/<int:item>/history/", views.option_history, name="option_history"),
    path("accounts/<int:pk>/reports/", views.reports, name="reports"),
    path(
        "accounts/<int:pk>/reports/email/",
        views.report_email_settings,
        name="report_email_settings",
    ),
    path("accounts/<int:pk>/switch/", bot_settings, {"action": "switch"}, name="bot_switch"),
    path("accounts/<int:pk>/risk/", bot_settings, {"action": "risk"}, name="bot_risk"),
    path("profile/", profiles.profile_page, name="profile"),
    path("profile/avatar/", profiles.profile_avatar, name="profile_avatar"),
    path("reports/strategies/", strategy_comparison, name="strategy_comparison"),
    path("signals/", views.all_signals, name="all_signals"),
    path("accounts/<int:pk>/delete/", views.delete_account, name="delete_account"),
]

if settings.ADMIN_ROUTE_ENABLED:
    urlpatterns.append(path("admin/", admin.site.urls))
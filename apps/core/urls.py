from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("my-work/", views.my_work, name="my_work"),
    path("search/", views.search, name="search"),
    path("notifications/", views.notifications, name="notifications"),
    path("notifications/read/", views.notifications_read, name="notifications_read"),
    path("notifications/<int:pk>/", views.notification_open, name="notification_open"),
    path("audit/", views.audit_log, name="audit_log"),
    path("help/", views.help_page, name="help"),
    path("help/<slug:topic>/", views.help_page, name="help_topic"),
    path("setup/", views.setup, name="setup"),
    path("healthz", views.healthz, name="healthz"),
    path("branding/logo-<slug:variant>", views.logo, name="logo"),
    path("system/", views.system_page, name="system"),
    path("system/action/", views.system_action, name="system_action"),
    path("system/<slug:tab>/", views.system_page, name="system_tab"),
]

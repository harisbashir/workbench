from django.urls import include, path

urlpatterns = [
    path("", include("apps.core.urls")),
    path("accounts/", include("apps.accounts.urls")),
    path("projects/", include("apps.projects.urls")),
    path("chat/", include("apps.chat.urls")),
    path("parts/", include("apps.inventory.urls")),
    path("production/", include("apps.production.urls")),
    path("integrations/", include("apps.integrations.urls")),
    path("files/", include("apps.files.urls")),
    path("", include("apps.firmware.urls")),
    path("", include("apps.design.urls")),
    path("time/", include("apps.timesheets.urls")),
]

handler403 = "apps.core.views.error_403"
handler404 = "apps.core.views.error_404"

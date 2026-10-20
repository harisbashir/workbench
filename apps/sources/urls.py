from django.urls import path

from . import views

app_name = "sources"

urlpatterns = [
    path("sources/", views.overview, name="overview"),
    path("projects/<str:key>/sources/", views.project_sources, name="project"),
    path("projects/<str:key>/sources/new/", views.repo_edit, name="repo_create"),
    path("projects/<str:key>/sources/<slug:slug>/", views.repo_detail, name="repo"),
    path("projects/<str:key>/sources/<slug:slug>/edit/", views.repo_edit, name="repo_edit"),
    path("projects/<str:key>/sources/<slug:slug>/delete/", views.repo_delete, name="repo_delete"),
    path("projects/<str:key>/sources/<slug:slug>/file/<int:v>/<path:path>", views.file_view, name="file"),
    path("projects/<str:key>/sources/<slug:slug>/zip/<int:v>/", views.version_zip, name="version_zip"),
    path("projects/<str:key>/sources/<slug:slug>/compare/", views.compare, name="compare"),
    path("projects/<str:key>/sources/<slug:slug>/checkin/", views.checkin_start, name="checkin"),
    path("projects/<str:key>/sources/<slug:slug>/checkin/<int:draft>/", views.checkin_preview, name="checkin_preview"),
    path("projects/<str:key>/sources/<slug:slug>/lock/", views.lock_action, name="lock"),
    path("projects/<str:key>/sources/<slug:slug>/promote/<int:v>/", views.promote, name="promote"),
]

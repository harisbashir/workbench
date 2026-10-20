from django.urls import path

from . import board_views, views

app_name = "firmware"

urlpatterns = [
    path("firmware/", views.overview, name="overview"),
    path("firmware/artifact/<int:pk>/", views.artifact_download, name="artifact"),
    path("projects/<str:key>/firmware/", views.project_firmware, name="project"),
    path("projects/<str:key>/firmware/new/", views.firmware_edit, name="create"),
    path("projects/<str:key>/firmware/<slug:slug>/", views.firmware_detail, name="detail"),
    path("projects/<str:key>/firmware/<slug:slug>/edit/", views.firmware_edit, name="edit"),
    path("projects/<str:key>/firmware/<slug:slug>/releases/new/", views.release_create, name="release_create"),
    path("projects/<str:key>/firmware/<slug:slug>/releases/<str:version>/", views.release_detail, name="release"),
    path("projects/<str:key>/firmware/<slug:slug>/releases/<str:version>/edit/", views.release_edit, name="release_edit"),
    path("projects/<str:key>/firmware/<slug:slug>/releases/<str:version>/upload/", views.release_upload, name="release_upload"),
    path("projects/<str:key>/firmware/<slug:slug>/releases/<str:version>/status/", views.release_status, name="release_status"),
    path("projects/<str:key>/firmware/<slug:slug>/releases/<str:version>/files/<int:pk>/delete/", views.artifact_delete, name="artifact_delete"),
    path("projects/<str:key>/boards/<int:board>/controllers/new/", board_views.controller_edit, name="controller_create"),
    path("projects/<str:key>/boards/<int:board>/controllers/<int:pk>/edit/", board_views.controller_edit, name="controller_edit"),
    path("projects/<str:key>/boards/<int:board>/controllers/<int:pk>/delete/", board_views.controller_delete, name="controller_delete"),
    path("projects/<str:key>/revisions/<int:pk>/firmware/", board_views.revision_set, name="revision_set"),
    path("projects/<str:key>/revisions/<int:pk>/firmware/lock/", board_views.revision_set_lock, name="revision_set_lock"),
    path("projects/<str:key>/revisions/<int:pk>/firmware/bundle/", board_views.revision_bundle, name="revision_bundle"),
    path("production/builds/<int:pk>/firmware-bundle/", board_views.build_bundle, name="build_bundle"),
]

from django.urls import path

from . import views

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
]

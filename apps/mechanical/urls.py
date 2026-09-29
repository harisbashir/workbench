from django.urls import path

from . import views

app_name = "mechanical"

P = "projects/<str:key>/mechanical/"

urlpatterns = [
    path(P + "new/", views.part_edit, name="create"),
    path(P + "<int:pk>/", views.part_detail, name="detail"),
    path(P + "<int:pk>/edit/", views.part_edit, name="edit"),
    path(P + "<int:pk>/upload/", views.upload, name="upload"),
    path(P + "<int:pk>/download-all/", views.download_all, name="download_all"),
    path(P + "<int:pk>/files/<int:file_id>/", views.file_detail, name="file"),
    path(P + "<int:pk>/files/<int:file_id>/download/", views.download, name="download"),
    path(P + "<int:pk>/files/<int:file_id>/action/", views.file_action, name="file_action"),
]

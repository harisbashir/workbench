from django.urls import path

from . import views

app_name = "files"

urlpatterns = [
    path("", views.index, name="index"),
    path("trash/", views.trash, name="trash"),
    path("trash/<int:pk>/", views.trash_action, name="trash_action"),
    path("storage/", views.storage, name="storage"),
    path("storage/prune-versions/", views.prune_versions, name="prune_versions"),
    path("storage/empty-trash/", views.empty_trash, name="empty_trash"),
    path("doc/<int:pk>/", views.document, name="document"),
    path("doc/<int:pk>/edit/", views.document_edit, name="document_edit"),
    path("doc/<int:pk>/version/", views.document_new_version, name="document_new_version"),
    path("doc/<int:pk>/delete/", views.document_delete, name="document_delete"),
    path("download/<int:pk>/", views.download, name="download"),
    path("<str:space>/", views.folder_view, name="space"),
    path("<str:space>/upload/", views.upload, name="upload_root"),
    path("<str:space>/new-folder/", views.folder_create, name="folder_create_root"),
    path("<str:space>/f/<int:folder_id>/", views.folder_view, name="folder"),
    path("<str:space>/f/<int:folder_id>/upload/", views.upload, name="upload"),
    path("<str:space>/f/<int:folder_id>/new-folder/", views.folder_create, name="folder_create"),
    path("<str:space>/f/<int:folder_id>/action/", views.folder_action, name="folder_action"),
]

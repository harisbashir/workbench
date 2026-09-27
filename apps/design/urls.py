from django.urls import path

from . import views

app_name = "design"

R = "projects/<str:key>/revisions/<int:pk>/design/"

urlpatterns = [
    path(R, views.design_files, name="files"),
    path(R + "upload/", views.upload, name="upload"),
    path(R + "download-all/", views.download_all, name="download_all"),
    path(R + "pcb/", views.pcb_viewer, name="pcb"),
    path(R + "pcb.svg", views.pcb_svg, name="pcb_svg"),
    path(R + "files/<int:file_id>/", views.file_detail, name="file"),
    path(R + "files/<int:file_id>/download/", views.download, name="download"),
    path(R + "files/<int:file_id>/delete/", views.delete, name="delete"),
    path(R + "files/<int:file_id>/category/", views.set_category, name="set_category"),
]

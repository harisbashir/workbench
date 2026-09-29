from django.urls import path, re_path

from . import views

app_name = "diagrams"

D = "projects/<str:key>/diagrams/"

urlpatterns = [
    path(D + "new/", views.create, name="create"),
    path(D + "<int:pk>/", views.detail, name="detail"),
    path(D + "<int:pk>/edit/", views.edit, name="edit"),
    path(D + "<int:pk>/save/", views.save, name="save"),
    path(D + "<int:pk>/status/", views.status, name="status"),
    path(D + "<int:pk>/comment/", views.comment, name="comment"),
    path(D + "<int:pk>/settings/", views.settings_, name="settings"),
    path(D + "<int:pk>/preview.pdf", views.preview_pdf, name="preview_pdf"),
    re_path(r"^projects/(?P<key>[^/]+)/diagrams/(?P<pk>\d+)/v(?P<number>\d+)\.(?P<fmt>svg|pdf|json)$", views.export, name="export"),
]

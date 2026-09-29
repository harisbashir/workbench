from django.urls import path

from . import views

app_name = "cad"

urlpatterns = [
    path("3d/<slug:kind>/<int:pk>/mesh", views.mesh, name="mesh"),
    path("3d/<slug:kind>/<int:pk>/status", views.status, name="status"),
    path("3d/<slug:kind>/<int:pk>/preview.png", views.thumb, name="thumb"),
]

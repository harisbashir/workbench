from django.urls import path

from . import views

app_name = "production"

urlpatterns = [
    path("", views.build_list, name="build_list"),
    path("builds/new/", views.build_create, name="build_create"),
    path("builds/<int:pk>/", views.build_detail, name="build_detail"),
    path("builds/<int:pk>/action/", views.build_action, name="build_action"),
    path("orders/", views.po_list, name="po_list"),
    path("orders/new/", views.po_create, name="po_create"),
    path("orders/<int:pk>/", views.po_detail, name="po_detail"),
    path("orders/<int:pk>/action/", views.po_action, name="po_action"),
]

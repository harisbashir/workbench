from django.urls import path

from . import fab_views, views

app_name = "production"

urlpatterns = [
    path("", views.build_list, name="build_list"),
    path("builds/new/", views.build_create, name="build_create"),
    path("builds/<int:pk>/", views.build_detail, name="build_detail"),
    path("builds/<int:pk>/action/", views.build_action, name="build_action"),
    path("builds/<int:pk>/stage/", fab_views.build_stage, name="build_stage"),
    path("builds/<int:pk>/steps/<int:step_pk>/", fab_views.build_step, name="build_step"),
    path("builds/<int:pk>/traveler/", fab_views.traveler, name="traveler"),
    path("fab/<int:pk>/", fab_views.fab_package, name="fab_package"),
    path("fab/<int:pk>/fitted/", fab_views.bom_fitted, name="bom_fitted"),
    path("orders/", views.po_list, name="po_list"),
    path("orders/new/", views.po_create, name="po_create"),
    path("orders/<int:pk>/", views.po_detail, name="po_detail"),
    path("orders/<int:pk>/action/", views.po_action, name="po_action"),
]

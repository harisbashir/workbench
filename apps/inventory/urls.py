from django.urls import path

from . import views

app_name = "inventory"

urlpatterns = [
    path("", views.part_list, name="part_list"),
    path("new/", views.part_edit, name="part_create"),
    path("export/", views.part_export, name="part_export"),
    path("<int:pk>/", views.part_detail, name="part_detail"),
    path("<int:pk>/edit/", views.part_edit, name="part_edit"),
    path("<int:pk>/adjust/", views.part_adjust, name="part_adjust"),
    path("suppliers/", views.supplier_list, name="supplier_list"),
    path("suppliers/new/", views.supplier_edit, name="supplier_create"),
    path("suppliers/<int:pk>/", views.supplier_edit, name="supplier_edit"),
    path("boms/", views.bom_index, name="bom_index"),
    path("boms/<int:pk>/", views.bom, name="bom"),
    path("boms/<int:pk>/import/", views.bom_import_view, name="bom_import"),
    path("boms/<int:pk>/export/", views.bom_export, name="bom_export"),
    path("boms/<int:pk>/compare/<int:other_pk>/", views.bom_compare, name="bom_compare"),
    path("boms/<int:pk>/lines/new/", views.bom_line_edit, name="bom_line_create"),
    path("boms/<int:pk>/lines/<int:line_pk>/", views.bom_line_edit, name="bom_line_edit"),
]

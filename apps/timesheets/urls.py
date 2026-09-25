from django.urls import path

from . import views

app_name = "time"

urlpatterns = [
    path("", views.my_time, name="mine"),
    path("<int:pk>/delete/", views.delete_entry, name="delete"),
    path("task/<str:key>/<int:number>/", views.log_for_task, name="log_task"),
    path("report/", views.weekly_report, name="report"),
    path("export.csv", views.export_csv, name="export"),
]

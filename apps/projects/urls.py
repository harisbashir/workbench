from django.urls import path

from . import views

app_name = "projects"

urlpatterns = [
    path("", views.project_list, name="list"),
    path("new/", views.project_edit, name="create"),
    path("<str:key>/", views.project_detail, name="detail"),
    path("<str:key>/edit/", views.project_edit, name="edit"),
    path("<str:key>/board/", views.board, name="board"),
    path("<str:key>/tasks/", views.task_list, name="task_list"),
    path("<str:key>/tasks/new/", views.task_create, name="task_create"),
    path("<str:key>/tasks/<int:number>/", views.task_detail, name="task"),
    path("<str:key>/tasks/<int:number>/edit/", views.task_edit, name="task_edit"),
    path("<str:key>/tasks/<int:number>/move/", views.task_move, name="task_move"),
    path("<str:key>/tasks/<int:number>/attach/", views.task_attach, name="task_attach"),
    path("<str:key>/tasks/<int:number>/delete/", views.task_delete, name="task_delete"),
    path("<str:key>/revisions/new/", views.revision_edit, name="revision_create"),
    path("<str:key>/revisions/<int:pk>/", views.revision_detail, name="revision"),
    path("<str:key>/revisions/<int:pk>/edit/", views.revision_edit, name="revision_edit"),
    path("<str:key>/revisions/<int:pk>/checklist/", views.revision_check, name="revision_check"),
]

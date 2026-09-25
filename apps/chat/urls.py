from django.urls import path

from . import views

app_name = "chat"

urlpatterns = [
    path("", views.index, name="index"),
    path("new/", views.channel_create, name="create"),
    path("direct/", views.direct, name="direct"),
    path("unread/", views.unread, name="unread"),
    path("message/<int:pk>/", views.edit_message, name="edit_message"),
    path("<slug:slug>/", views.channel_view, name="channel"),
    path("<slug:slug>/poll/", views.poll, name="poll"),
    path("<slug:slug>/post/", views.post_message, name="post"),
    path("<slug:slug>/upload/", views.upload, name="upload"),
    path("<slug:slug>/join/", views.channel_join, name="join"),
    path("<slug:slug>/leave/", views.channel_leave, name="leave"),
]

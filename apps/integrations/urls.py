from django.urls import path

from . import views

app_name = "integrations"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("github/webhook/", views.github_webhook, name="github_webhook"),
]

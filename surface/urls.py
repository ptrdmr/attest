"""URL routes for the Attest user and client surfaces."""

from django.urls import path

from . import views

app_name = "surface"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
    path("login/", views.MagicLinkRequestView.as_view(), name="login-request"),
    path("login/sent/", views.MagicLinkSentView.as_view(), name="login-sent"),
    path("login/<path:token>/", views.MagicLoginView.as_view(), name="magic-login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("projects/", views.ProjectListView.as_view(), name="project-list"),
    path("projects/new/", views.ProjectCreateView.as_view(), name="project-create"),
    path(
        "projects/<int:project_pk>/",
        views.ProjectDetailView.as_view(),
        name="project-detail",
    ),
    path(
        "projects/<int:project_pk>/edit/",
        views.ProjectUpdateView.as_view(),
        name="project-update",
    ),
    path(
        "projects/<int:project_pk>/delete/",
        views.ProjectDeleteView.as_view(),
        name="project-delete",
    ),
    path(
        "projects/<int:project_pk>/criteria/add/",
        views.AcceptanceItemCreateView.as_view(),
        name="criterion-create",
    ),
    path(
        "projects/<int:project_pk>/criteria/<int:item_pk>/edit/",
        views.AcceptanceItemUpdateView.as_view(),
        name="criterion-update",
    ),
    path(
        "projects/<int:project_pk>/criteria/<int:item_pk>/delete/",
        views.AcceptanceItemDeleteView.as_view(),
        name="criterion-delete",
    ),
    path(
        "projects/<int:project_pk>/submit/",
        views.SubmitCriteriaView.as_view(),
        name="criteria-submit",
    ),
    path(
        "client/review/<path:token>/approve/",
        views.ClientApproveView.as_view(),
        name="client-approve",
    ),
    path(
        "client/review/<path:token>/changes/",
        views.ClientRequestChangesView.as_view(),
        name="client-request-changes",
    ),
    path("client/review/<path:token>/", views.ClientReviewView.as_view(), name="client-review"),
    path(
        "record/",
        views.PlaceholderView.as_view(),
        {"title": "Record"},
        name="record",
    ),
    path(
        "billing/",
        views.PlaceholderView.as_view(),
        {"title": "Billing"},
        name="billing",
    ),
]

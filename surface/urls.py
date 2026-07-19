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
        "projects/<int:project_pk>/ai-draft/",
        views.AiDraftGenerateView.as_view(),
        name="ai-draft-generate",
    ),
    path(
        "projects/<int:project_pk>/ai-draft/confirm/",
        views.AiDraftConfirmView.as_view(),
        name="ai-draft-confirm",
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
        "projects/<int:project_pk>/change-orders/add/",
        views.ChangeOrderCreateView.as_view(),
        name="change-order-create",
    ),
    path(
        "projects/<int:project_pk>/delivery/<int:item_pk>/",
        views.DeliveryItemUpdateView.as_view(),
        name="delivery-item-update",
    ),
    path(
        "projects/<int:project_pk>/deliver/",
        views.MarkDeliveredView.as_view(),
        name="mark-delivered",
    ),
    path(
        "projects/<int:project_pk>/signing-link/",
        views.SendSignLinkView.as_view(),
        name="send-sign-link",
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
        "client/sign/<path:token>/",
        views.ClientSignView.as_view(),
        name="client-sign",
    ),
    path(
        "client/change-order/<path:token>/",
        views.ClientChangeOrderView.as_view(),
        name="client-change-order",
    ),
    path(
        "u/<slug:handle>/",
        views.PublicRecordView.as_view(),
        name="public-record",
    ),
    path(
        "record/",
        views.RecordRedirectView.as_view(),
        name="record",
    ),
    path(
        "billing/",
        views.BillingView.as_view(),
        name="billing",
    ),
    path(
        "billing/activate-pro/",
        views.BillingActionView.as_view(grant_type="pro"),
        name="billing-activate-pro",
    ),
    path(
        "billing/buy-project-pack/",
        views.BillingActionView.as_view(grant_type="project_pack"),
        name="billing-buy-project-pack",
    ),
]

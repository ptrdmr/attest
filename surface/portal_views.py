"""Read-only project displays for verified client portal sessions."""

from django.shortcuts import get_object_or_404
from django.views.generic import TemplateView

from ledger.models import AcceptanceItem, Project

from .client_auth import ClientSessionMixin
from .delivery import _delivery_rows

CLIENT_STATUS_LABELS = {
    Project.Status.CRITERIA_PENDING: "Awaiting your approval",
    Project.Status.ACTIVE: "Work in progress",
    Project.Status.DELIVERED: "Delivered — awaiting your signature",
    Project.Status.ATTESTED: "Signed",
    Project.Status.DISPUTED: "Disputed",
}


def client_status_label(status):
    """Return the deliberate client-facing wording for a project status."""
    return CLIENT_STATUS_LABELS.get(status, status)


def _signed_delivery_rows(attestation):
    """Build shared-template rows only from one attestation's frozen payload."""
    if attestation is None:
        return []
    rows = []
    for item in attestation.payload.get("acceptance_items", []):
        steps = item.get("steps", [])
        rows.append(
            {
                "item": item,
                "steps": steps,
                "has_undone_steps": any(not step.get("is_done") for step in steps),
            }
        )
    return rows


class ClientProjectMixin:
    """Resolve a visible project belonging to the verified client or raise 404."""

    project = None

    def dispatch(self, request, *args, **kwargs):
        """Attach only a non-draft, case-insensitively matched client project."""
        self.project = get_object_or_404(
            Project.objects.select_related("owner").exclude(
                status=Project.Status.DRAFT
            ),
            pk=kwargs["project_pk"],
            client_email__iexact=self.client_email,
        )
        return super().dispatch(request, *args, **kwargs)


class ClientPortalView(ClientSessionMixin, TemplateView):
    """List every non-draft project for the verified client email."""

    template_name = "surface/portal/list.html"

    def get_context_data(self, **kwargs):
        """Add matched projects with freelancer and client-facing status labels."""
        context = super().get_context_data(**kwargs)
        projects = (
            Project.objects.filter(client_email__iexact=self.client_email)
            .exclude(status=Project.Status.DRAFT)
            .select_related("owner")
            .order_by("-created_at", "-pk")
        )
        context["project_rows"] = [
            {"project": project, "status_label": client_status_label(project.status)}
            for project in projects
        ]
        return context


class ClientPortalProjectView(
    ClientSessionMixin,
    ClientProjectMixin,
    TemplateView,
):
    """Show one client's project state without exposing mutating controls."""

    template_name = "surface/portal/detail.html"

    def get_context_data(self, **kwargs):
        """Add live scope, progress, history, and any frozen signed record."""
        context = super().get_context_data(**kwargs)
        acceptance_rows = [
            row
            for row in _delivery_rows(self.project)
            if row["item"].state != AcceptanceItem.State.DRAFT
        ]
        attestation = self.project.attestations.filter(is_current=True).first()
        context.update(
            {
                "project": self.project,
                "status_label": client_status_label(self.project.status),
                "acceptance_rows": acceptance_rows,
                "change_orders": self.project.change_orders.order_by(
                    "-created_at", "-pk"
                ),
                "attestation": attestation,
                "signed_rows": _signed_delivery_rows(attestation),
            }
        )
        return context

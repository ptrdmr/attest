"""Project displays and consent actions for verified client portal sessions."""

from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views import View
from django.views.generic import TemplateView

from ledger import services
from ledger.models import AcceptanceItem, Project

from .client_auth import ClientSessionMixin
from .consent import (
    APPROVAL_ACCEPTED,
    APPROVAL_STALE,
    _approve_submitted_batch,
    _sign_delivery_record,
    _submitted_batch_fingerprint,
)
from .delivery import _delivery_rows
from .forms import ClientApprovalForm, SignatureForm

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
        context["portal_page"] = True
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
                "has_submitted_items": any(
                    row["item"].state == AcceptanceItem.State.SUBMITTED
                    for row in acceptance_rows
                ),
                "portal_page": True,
            }
        )
        return context


class PortalApproveView(
    ClientSessionMixin,
    ClientProjectMixin,
    View,
):
    """Review and approve submitted scope from a verified client session."""

    template_name = "surface/portal/approve.html"

    def get(self, request, project_pk):
        """Render the current submitted batch and its consent fingerprint."""
        return self._render(request)

    def post(self, request, project_pk):
        """Validate and approve the exact submitted batch the client reviewed."""
        form = ClientApprovalForm(request.POST)
        if not form.is_valid():
            return self._render(request, stale_batch=True)
        outcome = _approve_submitted_batch(
            self.project,
            form.cleaned_data["batch_fingerprint"],
        )
        if outcome == APPROVAL_ACCEPTED:
            return self._render(request, approved=True)
        return self._render(
            request,
            stale_batch=outcome == APPROVAL_STALE,
        )

    def _render(self, request, *, stale_batch=False, approved=False):
        """Render current scope with exactly one approval outcome."""
        self.project.refresh_from_db()
        pending_items = list(
            self.project.acceptance_items.filter(
                state=AcceptanceItem.State.SUBMITTED
            ).prefetch_related("steps")
        )
        return render(
            request,
            self.template_name,
            {
                "project": self.project,
                "pending_acceptance_items": pending_items,
                "approval_form": ClientApprovalForm(
                    initial={
                        "batch_fingerprint": _submitted_batch_fingerprint(
                            pending_items
                        )
                    }
                ),
                "stale_batch": stale_batch,
                "approved": approved,
                "portal_page": True,
            },
        )


class PortalSignView(
    ClientSessionMixin,
    ClientProjectMixin,
    View,
):
    """Show and sign a delivery record from a verified client session."""

    template_name = "surface/portal/sign.html"
    signed_template_name = "surface/portal/signed.html"

    def get(self, request, project_pk):
        """Render a delivered record or its existing signed confirmation."""
        if self.project.status == Project.Status.ATTESTED:
            return self._already_signed(request)
        if self.project.status != Project.Status.DELIVERED:
            raise Http404
        return self._render(request, SignatureForm())

    def post(self, request, project_pk):
        """Bind the verified client identity and delegate signing to Ledger."""
        if self.project.status == Project.Status.ATTESTED:
            return self._already_signed(request)
        if self.project.status != Project.Status.DELIVERED:
            raise Http404
        form = SignatureForm(request.POST)
        if not form.is_valid():
            return self._render(request, form)
        try:
            attestation = _sign_delivery_record(
                project=self.project,
                client_email=self.project.client_email,
                client_name_typed=form.cleaned_data["signature_name"],
                signature_meta={},
            )
        except services.InvalidTransition:
            self.project.refresh_from_db()
            if self.project.status == Project.Status.ATTESTED:
                return self._already_signed(request)
            return self._render(
                request,
                form=None,
                signing_unavailable=True,
            )
        return render(
            request,
            self.signed_template_name,
            {
                "project": self.project,
                "attestation": attestation,
                "portal_page": True,
            },
        )

    def _already_signed(self, request):
        """Show the current signed record without invoking Ledger again."""
        attestation = self.project.attestations.filter(is_current=True).first()
        return render(
            request,
            self.signed_template_name,
            {
                "project": self.project,
                "attestation": attestation,
                "portal_page": True,
            },
        )

    def _render(self, request, form, *, signing_unavailable=False):
        """Render delivery evidence using the shared state-sensitive partial."""
        return render(
            request,
            self.template_name,
            {
                "project": self.project,
                "acceptance_rows": _delivery_rows(self.project),
                "form": form,
                "signing_unavailable": signing_unavailable,
                "portal_page": True,
            },
        )

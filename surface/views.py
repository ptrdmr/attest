"""HTTP views for projects, delivery, signing, and public records."""

import smtplib

from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core import signing
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import FormView, TemplateView

from ledger import services
from ledger.models import AcceptanceItem, ChangeOrder, Profile, Project

from . import billing
from .auth import (
    ensure_profile,
    get_or_create_freelancer,
    make_magic_login_token,
    read_magic_login_token,
)
from .forms import (
    AcceptanceItemForm,
    ActionForm,
    ChangeOrderDecisionForm,
    ChangeOrderForm,
    DeliveryItemForm,
    MagicLinkRequestForm,
    ProjectForm,
    SignatureForm,
)
from .tokens import (
    make_change_order_token,
    make_client_token,
    read_change_order_token,
    read_client_token,
)


def _is_htmx(request):
    """Return whether a request asks for an HTMX fragment."""
    return request.headers.get("HX-Request") == "true"


def _owned_project(request, project_pk):
    """Resolve a project owned by the authenticated freelancer or raise 404."""
    return get_object_or_404(Project, pk=project_pk, owner=ensure_profile(request.user))


def _send_email(request, subject, body, recipient):
    """Send one email; on delivery failure queue a friendly message.

    Returns whether the send succeeded. Failure details never reach the user.
    """
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=None,
            recipient_list=[recipient],
        )
    except (smtplib.SMTPException, OSError):
        messages.error(request, "We couldn't send the email — please try again.")
        return False
    return True


def _send_signing_link(request, project):
    """Email a purpose-bound signing URL without rendering its token."""
    sign_token = make_client_token(project, "sign")
    sign_url = request.build_absolute_uri(
        reverse("surface:client-sign", kwargs={"token": sign_token})
    )
    return _send_email(
        request,
        subject="Sign the project delivery record",
        body=sign_url,
        recipient=project.client_email,
    )


def _send_change_order_link(request, change_order):
    """Email a proposal URL without rendering its token."""
    token = make_change_order_token(change_order)
    review_url = request.build_absolute_uri(
        reverse("surface:client-change-order", kwargs={"token": token})
    )
    return _send_email(
        request,
        subject=f"Review a change order for {change_order.project.title}",
        body=review_url,
        recipient=change_order.project.client_email,
    )


def _project_context(project, **extra):
    """Build the common project-detail template context."""
    delivery_rows = [
        {"item": item, "form": DeliveryItemForm(instance=item)}
        for item in project.acceptance_items.all()
    ]
    context = {
        "project": project,
        "acceptance_items": project.acceptance_items.all(),
        "criteria_locked": services.criteria_locked(project),
        "acceptance_form": AcceptanceItemForm(),
        "change_order_form": ChangeOrderForm(),
        "change_orders": project.change_orders.order_by("-created_at", "-pk"),
        "has_open_change_orders": services.has_open_change_orders(project),
        "action_form": ActionForm(),
        "delivery_rows": delivery_rows,
        "all_delivery_reviewed": bool(delivery_rows)
        and all(row["item"].is_passed is not None for row in delivery_rows),
    }
    context.update(extra)
    return context


def _render_criteria(request, project, *, form=None, status=200):
    """Render the criteria HTMX fragment with optional form errors."""
    context = _project_context(project)
    if form is not None:
        context["acceptance_form"] = form
    return render(request, "surface/partials/criteria.html", context, status=status)


def _client_token_error(request):
    """Return the friendly gone page for an invalid or expired client token."""
    return render(request, "surface/client/token_error.html", status=410)


class HomeView(View):
    """Route visitors to login and authenticated users to their projects."""

    def get(self, request):
        """Redirect according to the current authentication state."""
        destination = (
            "surface:project-list"
            if request.user.is_authenticated
            else "surface:login-request"
        )
        return redirect(destination)


class MagicLinkRequestView(FormView):
    """Email a short-lived login link without disclosing account existence."""

    template_name = "surface/auth/request_link.html"
    form_class = MagicLinkRequestForm
    success_url = reverse_lazy("surface:login-sent")

    def form_valid(self, form):
        """Create the freelancer if needed and send a login URL only."""
        user = get_or_create_freelancer(form.cleaned_data["email"])
        token = make_magic_login_token(user)
        login_url = self.request.build_absolute_uri(
            reverse("surface:magic-login", kwargs={"token": token})
        )
        if not _send_email(
            self.request,
            subject="Your Attest login link",
            body=login_url,
            recipient=user.email,
        ):
            return redirect("surface:login-request")
        return super().form_valid(form)


class MagicLinkSentView(TemplateView):
    """Show the same confirmation after every login-link request."""

    template_name = "surface/auth/link_sent.html"


class MagicLoginView(View):
    """Authenticate a freelancer from a valid, unexpired signed token."""

    def get(self, request, token):
        """Validate the token, establish a session, and redirect."""
        try:
            user = read_magic_login_token(token)
        except signing.SignatureExpired:
            return render(request, "surface/auth/link_error.html")
        except (signing.BadSignature, get_user_model().DoesNotExist):
            return render(request, "surface/auth/link_error.html")
        login(request, user)
        return redirect("surface:project-list")


class LogoutView(View):
    """End the freelancer's authenticated session."""

    def post(self, request):
        """Log out and return to the magic-link request page."""
        logout(request)
        return redirect("surface:login-request")


class OwnedProjectMixin(LoginRequiredMixin):
    """Resolve the URL project only within the current freelancer's projects."""

    project = None

    def dispatch(self, request, *args, **kwargs):
        """Attach the owned project before handling the request."""
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        self.project = _owned_project(request, kwargs["project_pk"])
        if not self.project_is_accessible():
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def project_is_accessible(self):
        """Return whether this view permits the project's current state."""
        return True


class ProjectListView(LoginRequiredMixin, TemplateView):
    """List projects owned by the authenticated freelancer."""

    template_name = "surface/projects/list.html"

    def get_context_data(self, **kwargs):
        """Add the current freelancer's projects to the template."""
        context = super().get_context_data(**kwargs)
        profile = ensure_profile(self.request.user)
        context["projects"] = profile.projects.order_by("-created_at")
        return context


class ProjectCreateView(LoginRequiredMixin, FormView):
    """Create a draft project owned by the authenticated freelancer."""

    template_name = "surface/projects/form.html"
    form_class = ProjectForm

    def dispatch(self, request, *args, **kwargs):
        """Require a Pro subscription or unused project-pack credit."""
        if request.user.is_authenticated and not billing.has_project_entitlement(request):
            messages.info(request, "Choose a plan before creating a project.")
            return redirect("surface:billing")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        """Consume entitlement first, then persist the validated draft project."""
        if not billing.consume_project_entitlement(self.request):
            messages.info(self.request, "Choose a plan before creating a project.")
            return redirect("surface:billing")
        project = form.save(commit=False)
        project.owner = ensure_profile(self.request.user)
        try:
            project.save()
        except (IntegrityError, ValueError, TypeError):
            billing.restore_project_pack_credit(self.request)
            raise
        messages.success(self.request, "Project created.")
        return redirect("surface:project-detail", project_pk=project.pk)


class ProjectDetailView(OwnedProjectMixin, TemplateView):
    """Show project state, criteria, and currently legal actions."""

    template_name = "surface/projects/detail.html"

    def get_context_data(self, **kwargs):
        """Add project detail and criteria editing state."""
        context = super().get_context_data(**kwargs)
        context.update(_project_context(self.project))
        return context


class ProjectUpdateView(OwnedProjectMixin, FormView):
    """Edit project details while the project remains a draft."""

    template_name = "surface/projects/form.html"
    form_class = ProjectForm

    def project_is_accessible(self):
        """Permit project detail edits only in the draft state."""
        return self.project.status == Project.Status.DRAFT

    def get_form_kwargs(self):
        """Bind the form to the owned project."""
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.project
        return kwargs

    def form_valid(self, form):
        """Persist validated draft project edits."""
        form.save()
        messages.success(self.request, "Project updated.")
        return redirect("surface:project-detail", project_pk=self.project.pk)


class ProjectDeleteView(OwnedProjectMixin, View):
    """Delete an owned draft project after an explicit POST."""

    def post(self, request, project_pk):
        """Validate the action and delete only a draft project."""
        form = ActionForm(request.POST)
        if not form.is_valid() or self.project.status != Project.Status.DRAFT:
            raise Http404
        self.project.delete()
        messages.success(request, "Draft project deleted.")
        return redirect("surface:project-list")


class AcceptanceItemCreateView(OwnedProjectMixin, View):
    """Add an acceptance item while project criteria are editable."""

    def post(self, request, project_pk):
        """Validate and create an ordered acceptance item."""
        if services.criteria_locked(self.project):
            raise Http404
        item = AcceptanceItem(project=self.project)
        form = AcceptanceItemForm(request.POST, instance=item)
        if form.is_valid():
            try:
                # Savepoint keeps any enclosing transaction usable after the
                # unique-order constraint fires (e.g. under ATOMIC_REQUESTS).
                with transaction.atomic():
                    form.save()
            except IntegrityError:
                form.add_error("order", "That order is already in use.")
            else:
                return self._success_response(request)
        return self._error_response(request, form)

    def _error_response(self, request, form):
        """Show validation errors in an HTMX fragment or full detail page."""
        if _is_htmx(request):
            return _render_criteria(request, self.project, form=form)
        return render(
            request,
            "surface/projects/detail.html",
            _project_context(self.project, acceptance_form=form),
        )

    def _success_response(self, request):
        """Return the updated fragment or redirect to project detail."""
        if _is_htmx(request):
            return _render_criteria(request, self.project)
        return redirect("surface:project-detail", project_pk=self.project.pk)


class AcceptanceItemUpdateView(OwnedProjectMixin, View):
    """Edit one acceptance item belonging to an editable project."""

    def get(self, request, project_pk, item_pk):
        """Render an inline form for the selected criterion."""
        if services.criteria_locked(self.project):
            raise Http404
        item = get_object_or_404(AcceptanceItem, pk=item_pk, project=self.project)
        form = AcceptanceItemForm(instance=item)
        return render(
            request,
            "surface/partials/criterion_form.html",
            {"project": self.project, "item": item, "form": form},
        )

    def post(self, request, project_pk, item_pk):
        """Validate and persist criterion edits."""
        if services.criteria_locked(self.project):
            raise Http404
        item = get_object_or_404(AcceptanceItem, pk=item_pk, project=self.project)
        form = AcceptanceItemForm(request.POST, instance=item)
        if form.is_valid():
            try:
                # Savepoint keeps any enclosing transaction usable after the
                # unique-order constraint fires (e.g. under ATOMIC_REQUESTS).
                with transaction.atomic():
                    form.save()
            except IntegrityError:
                form.add_error("order", "That order is already in use.")
            else:
                return self._success_response(request)
        return self._error_response(request, item, form)

    def _error_response(self, request, item, form):
        """Show validation errors in an HTMX form or full detail page."""
        if _is_htmx(request):
            return render(
                request,
                "surface/partials/criterion_form.html",
                {"project": self.project, "item": item, "form": form},
            )
        return render(
            request,
            "surface/projects/detail.html",
            _project_context(self.project, acceptance_form=form),
        )

    def _success_response(self, request):
        """Return the updated fragment or redirect to project detail."""
        if _is_htmx(request):
            return _render_criteria(request, self.project)
        return redirect("surface:project-detail", project_pk=self.project.pk)


class AcceptanceItemDeleteView(OwnedProjectMixin, View):
    """Delete one acceptance item while criteria remain editable."""

    def post(self, request, project_pk, item_pk):
        """Validate and remove a criterion owned through the project."""
        if services.criteria_locked(self.project):
            raise Http404
        form = ActionForm(request.POST)
        if not form.is_valid():
            raise Http404
        item = get_object_or_404(AcceptanceItem, pk=item_pk, project=self.project)
        item.delete()
        if _is_htmx(request):
            return _render_criteria(request, self.project)
        return redirect("surface:project-detail", project_pk=self.project.pk)


class SubmitCriteriaView(OwnedProjectMixin, View):
    """Submit draft criteria and email the client review link."""

    def post(self, request, project_pk):
        """Transition through Ledger and present the generated review URL."""
        form = ActionForm(request.POST)
        if not form.is_valid() or not self.project.acceptance_items.exists():
            messages.error(request, "Add at least one acceptance criterion first.")
            return redirect("surface:project-detail", project_pk=self.project.pk)
        try:
            services.submit_criteria_for_approval(self.project)
        except services.InvalidTransition:
            messages.error(request, "This project cannot be sent for approval now.")
            return redirect("surface:project-detail", project_pk=self.project.pk)
        review_token = make_client_token(self.project, "review")
        review_url = request.build_absolute_uri(
            reverse("surface:client-review", kwargs={"token": review_token})
        )
        if _send_email(
            request,
            subject="Review project criteria",
            body=review_url,
            recipient=self.project.client_email,
        ):
            messages.success(request, "Criteria sent for client approval.")
        # Review token lives only in the email — never in the HTML response.
        return render(
            request,
            "surface/projects/detail.html",
            _project_context(self.project),
        )


class ChangeOrderCreateView(OwnedProjectMixin, View):
    """Propose an active-project change order and email it to the client."""

    def project_is_accessible(self):
        """Permit proposals only while project work is active."""
        return self.project.status == Project.Status.ACTIVE

    def post(self, request, project_pk):
        """Validate the proposal and delegate creation to Ledger."""
        form = ChangeOrderForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                "surface/projects/detail.html",
                _project_context(self.project, change_order_form=form),
            )
        try:
            change_order = services.propose_change_order(
                project=self.project,
                description=form.cleaned_data["description"],
                amount_cents=form.cleaned_data["amount_cents"],
                timeline_days=form.cleaned_data["timeline_days"],
            )
        except (services.InvalidTransition, ValueError):
            messages.error(request, "This change order cannot be proposed now.")
            return redirect("surface:project-detail", project_pk=self.project.pk)
        if _send_change_order_link(request, change_order):
            messages.success(request, "Change order sent for client review.")
        return redirect("surface:project-detail", project_pk=self.project.pk)


class DeliveryItemUpdateView(OwnedProjectMixin, View):
    """Record delivery status and evidence for one active criterion."""

    def project_is_accessible(self):
        """Permit delivery updates only while the project is active."""
        return self.project.status == Project.Status.ACTIVE

    def post(self, request, project_pk, item_pk):
        """Validate and persist the selected criterion's delivery result."""
        item = get_object_or_404(
            AcceptanceItem,
            pk=item_pk,
            project=self.project,
        )
        form = DeliveryItemForm(request.POST, instance=item)
        if form.is_valid():
            form.save()
            messages.success(request, "Delivery item updated.")
            return redirect("surface:project-detail", project_pk=self.project.pk)

        delivery_rows = [
            {
                "item": candidate,
                "form": form if candidate.pk == item.pk else DeliveryItemForm(instance=candidate),
            }
            for candidate in self.project.acceptance_items.all()
        ]
        return render(
            request,
            "surface/projects/detail.html",
            _project_context(self.project, delivery_rows=delivery_rows),
        )


class MarkDeliveredView(OwnedProjectMixin, View):
    """Mark a reviewed checklist delivered and email its signing link."""

    def project_is_accessible(self):
        """Permit delivery only from the active project state."""
        return self.project.status == Project.Status.ACTIVE

    def post(self, request, project_pk):
        """Validate completion, transition through Ledger, and invite signing."""
        form = ActionForm(request.POST)
        items = self.project.acceptance_items.all()
        if (
            not form.is_valid()
            or not items.exists()
            or items.filter(is_passed__isnull=True).exists()
        ):
            messages.error(
                request,
                "Review every delivery item before marking the project delivered.",
            )
            return redirect("surface:project-detail", project_pk=self.project.pk)
        try:
            services.mark_delivered(self.project)
        except services.InvalidTransition:
            if services.has_open_change_orders(self.project):
                message = "Resolve all proposed change orders before delivery."
            else:
                message = "This project cannot be marked delivered now."
            messages.error(request, message)
            return redirect("surface:project-detail", project_pk=self.project.pk)

        if _send_signing_link(request, self.project):
            messages.success(request, "Delivery recorded and sent for signature.")
        return redirect("surface:project-detail", project_pk=self.project.pk)


class SendSignLinkView(OwnedProjectMixin, View):
    """Resend a signing invitation for a delivered project."""

    def project_is_accessible(self):
        """Permit signing invitations only while awaiting signature."""
        return self.project.status == Project.Status.DELIVERED

    def post(self, request, project_pk):
        """Validate the action and email a fresh expiring signing link."""
        form = ActionForm(request.POST)
        if not form.is_valid():
            raise Http404
        if _send_signing_link(request, self.project):
            messages.success(request, "A fresh signing link was sent.")
        return redirect("surface:project-detail", project_pk=self.project.pk)


class ClientTokenMixin:
    """Resolve a client token to a project or return a friendly gone page."""

    token_purpose = None
    project = None

    def resolve_client_token(self, token):
        """Resolve this view's purpose-bound project token."""
        return read_client_token(token, self.token_purpose)

    def dispatch(self, request, *args, **kwargs):
        """Validate the path token before dispatching the client view."""
        try:
            self.project = self.resolve_client_token(kwargs["token"])
        except signing.SignatureExpired:
            return _client_token_error(request)
        except (signing.BadSignature, ChangeOrder.DoesNotExist, Project.DoesNotExist):
            return _client_token_error(request)
        return super().dispatch(request, *args, **kwargs)


class ClientChangeOrderView(ClientTokenMixin, View):
    """Show and resolve one purpose-bound change-order proposal."""

    token_purpose = "change_order"
    change_order = None

    def resolve_client_token(self, token):
        """Resolve the proposal and expose its owning project to the view."""
        self.change_order = read_change_order_token(token)
        return self.change_order.project

    def get(self, request, token):
        """Render the proposal without copying its token into the response."""
        return render(
            request,
            "surface/client/change_order.html",
            {
                "project": self.project,
                "change_order": self.change_order,
                "form": ChangeOrderDecisionForm(),
            },
        )

    def post(self, request, token):
        """Validate and delegate the client's decision to Ledger."""
        form = ChangeOrderDecisionForm(request.POST)
        if not form.is_valid():
            return _client_token_error(request)
        service = (
            services.approve_change_order
            if form.cleaned_data["decision"] == "approve"
            else services.decline_change_order
        )
        try:
            service(self.change_order)
        except services.InvalidTransition:
            messages.info(request, "This change order was already handled.")
        return render(
            request,
            "surface/client/change_order_thanks.html",
            {"change_order": self.change_order},
        )


class ClientReviewView(ClientTokenMixin, TemplateView):
    """Show project brief and criteria to a token-authenticated client."""

    token_purpose = "review"
    template_name = "surface/client/review.html"

    def get_context_data(self, **kwargs):
        """Add read-only project review details."""
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "project": self.project,
                "acceptance_items": self.project.acceptance_items.all(),
                "action_form": ActionForm(),
            }
        )
        return context


class ClientApproveView(ClientTokenMixin, View):
    """Approve criteria through the Ledger transition service."""

    token_purpose = "review"

    def post(self, request, token):
        """Validate the action and activate pending project criteria."""
        form = ActionForm(request.POST)
        if not form.is_valid():
            return _client_token_error(request)
        try:
            services.approve_criteria(self.project)
        except services.InvalidTransition:
            messages.info(request, "These criteria were already handled.")
        return render(request, "surface/client/thanks.html", {"project": self.project})


class ClientRequestChangesView(ClientTokenMixin, View):
    """Explain the MVP reply-by-email changes workflow without mutation."""

    token_purpose = "review"

    def post(self, request, token):
        """Validate the action and show non-mutating next steps."""
        form = ActionForm(request.POST)
        if not form.is_valid():
            return _client_token_error(request)
        messages.info(
            request,
            "Please reply to the freelancer's email with the changes you need.",
        )
        return redirect("surface:client-review", token=token)


class ClientSignView(ClientTokenMixin, View):
    """Show and sign a delivered project through a purpose-bound token."""

    token_purpose = "sign"
    template_name = "surface/client/sign.html"

    def get(self, request, token):
        """Render the immutable delivery preview and signature form."""
        if self.project.status == Project.Status.ATTESTED:
            return self._already_signed(request)
        if self.project.status != Project.Status.DELIVERED:
            return _client_token_error(request)
        return self._render(request, SignatureForm())

    def post(self, request, token):
        """Validate the typed signature and delegate signing to Ledger."""
        if self.project.status == Project.Status.ATTESTED:
            return self._already_signed(request)
        if self.project.status != Project.Status.DELIVERED:
            return _client_token_error(request)
        form = SignatureForm(request.POST)
        if not form.is_valid():
            return self._render(request, form)
        try:
            attestation = services.sign_attestation(
                project=self.project,
                client_email=self.project.client_email,
                client_name_typed=form.cleaned_data["signature_name"],
                signature_meta={},
            )
        except services.InvalidTransition:
            return _client_token_error(request)
        return render(
            request,
            "surface/client/signed.html",
            {"project": self.project, "attestation": attestation},
        )

    def _already_signed(self, request):
        """Show a friendly confirmation when the project is already attested."""
        attestation = self.project.attestations.filter(is_current=True).first()
        return render(
            request,
            "surface/client/signed.html",
            {"project": self.project, "attestation": attestation},
        )

    def _render(self, request, form):
        """Render delivery evidence without exposing the signing token."""
        return render(
            request,
            self.template_name,
            {
                "project": self.project,
                "acceptance_items": self.project.acceptance_items.all(),
                "form": form,
            },
        )


class PublicRecordView(TemplateView):
    """Render a freelancer's clean public attestations and dispute notice."""

    template_name = "surface/record/detail.html"

    def get_context_data(self, **kwargs):
        """Build public record data through Ledger derivation services."""
        context = super().get_context_data(**kwargs)
        profile = get_object_or_404(Profile, handle=kwargs["handle"])
        attestations = list(services.public_attestations(profile).order_by("-signed_at"))
        context.update(
            {
                "profile": profile,
                "attestations": attestations,
                "capability_tags": profile.capability_tags.all(),
                "last_shipped": attestations[0].signed_at if attestations else None,
                "disputed_count": services.disputed_count(profile),
            }
        )
        return context


class RecordRedirectView(LoginRequiredMixin, View):
    """Send a freelancer from the dashboard to their public record."""

    def get(self, request):
        """Resolve the current profile and redirect to its public URL."""
        profile = ensure_profile(request.user)
        return redirect("surface:public-record", handle=profile.handle)


class BillingView(LoginRequiredMixin, TemplateView):
    """Show the current session entitlement and available plans."""

    template_name = "surface/billing.html"

    def get_context_data(self, **kwargs):
        """Add plan labels, status, and local stub availability."""
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "pro_plan_label": billing.PRO_PLAN_LABEL,
                "project_pack_label": billing.PROJECT_PACK_LABEL,
                "has_pro_subscription": billing.has_pro_subscription(self.request),
                "project_pack_credits": billing.project_pack_credits(self.request),
                "stub_enabled": billing.billing_stub_enabled(),
            }
        )
        return context


class BillingActionView(LoginRequiredMixin, View):
    """Grant a local stub entitlement after an intentional POST."""

    grant_type = None

    def post(self, request):
        """Validate the request and activate the selected stub plan."""
        form = ActionForm(request.POST)
        if not form.is_valid():
            raise Http404
        grant = {
            "pro": billing.grant_pro_subscription,
            "project_pack": billing.grant_project_pack,
        }.get(self.grant_type)
        if grant is None or not grant(request):
            messages.error(request, "Local billing controls are not available.")
            return redirect("surface:billing")
        messages.success(request, "Billing entitlement activated.")
        return redirect("surface:billing")

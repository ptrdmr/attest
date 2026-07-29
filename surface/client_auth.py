"""Account-free client portal authentication and session views."""

from hashlib import sha256
import smtplib
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.core.cache import cache
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import FormView, TemplateView

from ledger.models import Project

from .forms import ClientPortalRequestForm
from .signed_links import (
    consume_token_once,
    prepare_magic_login_token,
    require_unconsumed_token,
)
from .tokens import PORTAL_TOKEN_MAX_AGE, make_portal_token, read_portal_token

CLIENT_EMAIL_SESSION_KEY = "attest_client_email"
CLIENT_VERIFIED_AT_SESSION_KEY = "attest_client_verified_at"
CLIENT_SESSION_MAX_AGE = 14 * 24 * 60 * 60
PORTAL_LOGIN_USED_NAMESPACE = "surface.portal-login.used"
PORTAL_REQUEST_LIMIT = 5
PORTAL_REQUEST_WINDOW = 60 * 60


def portal_link_request_allowed(email):
    """Atomically count portal-link requests for one email within an hour."""
    normalized_email = email.strip().lower()
    email_digest = sha256(normalized_email.encode("utf-8")).hexdigest()
    cache_key = f"surface.portal-login.requests:{email_digest}"
    if cache.add(cache_key, 1, timeout=PORTAL_REQUEST_WINDOW):
        return True
    try:
        request_count = cache.incr(cache_key)
    except ValueError:
        return cache.add(cache_key, 1, timeout=PORTAL_REQUEST_WINDOW)
    return request_count <= PORTAL_REQUEST_LIMIT


def read_unconsumed_portal_token(token):
    """Return the portal email if the token is valid and unused."""
    prepared_token = prepare_magic_login_token(token)
    email = read_portal_token(prepared_token)
    require_unconsumed_token(prepared_token, PORTAL_LOGIN_USED_NAMESPACE)
    return email


def read_and_consume_portal_token(token):
    """Return the portal email after atomically consuming the valid token."""
    prepared_token = prepare_magic_login_token(token)
    email = read_portal_token(prepared_token)
    consume_token_once(
        prepared_token,
        PORTAL_LOGIN_USED_NAMESPACE,
        PORTAL_TOKEN_MAX_AGE + 60,
    )
    return email


def clear_client_session(session):
    """Remove only client identity data from a shared browser session."""
    session.pop(CLIENT_EMAIL_SESSION_KEY, None)
    session.pop(CLIENT_VERIFIED_AT_SESSION_KEY, None)


def client_email_from_session(session):
    """Return an unexpired client email without mutating the supplied session."""
    email = session.get(CLIENT_EMAIL_SESSION_KEY)
    verified_at = session.get(CLIENT_VERIFIED_AT_SESSION_KEY)
    if (
        not isinstance(email, str)
        or not email
        or isinstance(verified_at, bool)
        or not isinstance(verified_at, (int, float))
        or timezone.now().timestamp() - verified_at > CLIENT_SESSION_MAX_AGE
    ):
        return None
    return email


def verified_client_email(request):
    """Return verified client email, clearing invalid client keys as a side effect."""
    email = client_email_from_session(request.session)
    if email is None:
        clear_client_session(request.session)
    return email


def _send_portal_link(request, email):
    """Email one portal link while keeping delivery errors generic."""
    token = make_portal_token(email)
    portal_url = request.build_absolute_uri(
        reverse("surface:portal-login") + "?" + urlencode({"token": token})
    )
    try:
        send_mail(
            subject="Your Attest client portal link",
            message=portal_url,
            from_email=None,
            recipient_list=[email],
        )
    except (smtplib.SMTPException, OSError):
        messages.error(request, "We couldn't send the email — please try again.")
        return False
    if settings.DEBUG and settings.EMAIL_BACKEND.endswith("console.EmailBackend"):
        request.session["attest_dev_portal_url"] = portal_url
    return True


class ClientSessionMixin:
    """Require an unexpired account-free client session."""

    client_email = None

    def dispatch(self, request, *args, **kwargs):
        """Attach the verified email or return to the portal request page."""
        self.client_email = verified_client_email(request)
        if self.client_email is None:
            return redirect("surface:portal-request")
        return super().dispatch(request, *args, **kwargs)


class ClientPortalRequestView(FormView):
    """Send portal links only to emails attached to visible projects."""

    template_name = "surface/portal/request_link.html"
    form_class = ClientPortalRequestForm
    success_url = reverse_lazy("surface:portal-sent")

    def form_valid(self, form):
        """Follow one enumeration-safe response path for every valid email."""
        email = form.cleaned_data["email"].strip().lower()
        self.request.session.pop("attest_dev_portal_url", None)
        is_allowed = portal_link_request_allowed(email)
        has_visible_project = Project.objects.filter(
            client_email__iexact=email
        ).exclude(status=Project.Status.DRAFT).exists()
        if is_allowed and has_visible_project:
            if not _send_portal_link(self.request, email):
                return redirect("surface:portal-request")
        return super().form_valid(form)


class ClientPortalSentView(TemplateView):
    """Show the same confirmation after every portal-link request."""

    template_name = "surface/portal/link_sent.html"

    def get_context_data(self, **kwargs):
        """Expose a one-time DEBUG portal URL for the console backend."""
        context = super().get_context_data(**kwargs)
        dev_portal_url = self.request.session.pop("attest_dev_portal_url", "")
        context["dev_portal_url"] = dev_portal_url
        return context


class ClientPortalLoginView(View):
    """Confirm a portal token before establishing the client session."""

    template_name = "surface/portal/login_confirm.html"
    error_template_name = "surface/portal/link_error.html"

    def get(self, request):
        """Validate without consuming, protecting links from scanner prefetch."""
        token = request.GET.get("token", "")
        try:
            read_unconsumed_portal_token(token)
        except (signing.BadSignature, signing.SignatureExpired):
            return render(request, self.error_template_name)
        return render(request, self.template_name, {"token": token})

    def post(self, request):
        """Consume the token, rotate the session key, and store client identity."""
        token = request.POST.get("token", "")
        try:
            email = read_and_consume_portal_token(token)
        except (signing.BadSignature, signing.SignatureExpired):
            return render(request, self.error_template_name)
        request.session.cycle_key()
        request.session[CLIENT_EMAIL_SESSION_KEY] = email
        request.session[CLIENT_VERIFIED_AT_SESSION_KEY] = timezone.now().timestamp()
        return redirect("surface:portal")


class ClientPortalLogoutView(View):
    """End only the client identity carried by the current session."""

    def post(self, request):
        """Clear client keys while preserving freelancer and billing state."""
        clear_client_session(request.session)
        return redirect("surface:portal-request")


class ClientPortalView(ClientSessionMixin, TemplateView):
    """List every non-draft project for the verified client email."""

    template_name = "surface/portal/list.html"

    def get_context_data(self, **kwargs):
        """Add case-insensitively matched projects with freelancer attribution."""
        context = super().get_context_data(**kwargs)
        context["projects"] = (
            Project.objects.filter(client_email__iexact=self.client_email)
            .exclude(status=Project.Status.DRAFT)
            .select_related("owner")
            .order_by("-created_at", "-pk")
        )
        return context

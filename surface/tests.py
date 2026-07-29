"""Verifier tests for the Surface layer (M2 + M3 + M5)."""

import ast
from datetime import datetime, timedelta, UTC
from hashlib import sha256
import inspect
import json
import re
from unittest.mock import patch
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.core.cache.backends.db import DatabaseCache
from django.core import mail, signing
from django.db import connection
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from ledger.models import (
    AcceptanceItem,
    AcceptanceStep,
    Attestation,
    ChangeOrder,
    Profile,
    Project,
)
from ledger.services import (
    InvalidTransition,
    amend_attestation,
    approve_criteria,
    compute_payload_hash,
    flag_dispute,
    mark_delivered,
    set_profile_visibility,
    sign_attestation,
    submit_criteria_for_approval,
    suspend_acceptance_item,
    withdraw_acceptance_item,
)
from surface import billing
from surface import client_auth
from surface import portal_views
from surface.auth import (
    MAGIC_LOGIN_MAX_AGE,
    MAGIC_LOGIN_SALT,
    MAGIC_LOGIN_USED_NAMESPACE,
    get_or_create_freelancer,
    make_magic_login_token,
    read_and_consume_magic_login_token,
)
from surface.tokens import (
    CLIENT_TOKEN_PURPOSES,
    CLIENT_TOKEN_MAX_AGE,
    PORTAL_TOKEN_MAX_AGE,
    PORTAL_TOKEN_SALT,
    make_change_order_token,
    make_client_token,
    make_portal_token,
    read_client_token,
    read_portal_token,
)


_profile_counter = 0

LOCMem_EMAIL = {"EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend"}

BILLING_STUB_SETTINGS = {"ATTEST_BILLING_STUB_MODE": True}


def make_profile(handle=None, email=None):
    """Create a user and profile for surface HTTP tests."""
    global _profile_counter
    _profile_counter += 1
    if handle is None:
        handle = f"freelancer-{_profile_counter}"
    if email is None:
        email = f"{handle}@example.com"
    user = get_user_model().objects.create_user(
        username=email,
        email=email,
        password="testpass",
    )
    return Profile.objects.create(
        user=user,
        handle=handle,
        display_name="Test Freelancer",
    )


def make_draft_project(owner=None, *, with_criteria=True):
    """Create a draft project, optionally with one acceptance criterion."""
    if owner is None:
        owner = make_profile()
    project = Project.objects.create(
        owner=owner,
        title="Surface Test Project",
        client_name="Acme Corp",
        client_email="client@acme.com",
        brief="Build a widget",
        skills_csv="django",
        status=Project.Status.DRAFT,
    )
    if with_criteria:
        AcceptanceItem.objects.create(
            project=project,
            text="First criterion",
            order=1,
        )
    return project


def project_form_data(**overrides):
    """Return valid POST data for ProjectForm."""
    data = {
        "title": "New Project",
        "client_name": "Client Co",
        "client_email": "client@example.com",
        "brief": "Project brief text",
        "skills_csv": "python",
        "revision_limit": 2,
    }
    data.update(overrides)
    return data


def grant_session_entitlement(client, *, pro=False, pack_credits=0):
    """Set session billing entitlement for HTTP tests."""
    session = client.session
    if pro:
        session[billing._PRO_SESSION_KEY] = True
    if pack_credits:
        session[billing._PACK_CREDITS_SESSION_KEY] = pack_credits
    session.save()


def login_as(client, profile, *, grant_entitlement=True):
    """Establish an authenticated session as the given profile's user."""
    client.force_login(profile.user)
    if grant_entitlement:
        grant_session_entitlement(client, pro=True)


def request_magic_link(client, email):
    """POST a magic-link request and return the response."""
    return client.post(reverse("surface:login-request"), {"email": email})


def login_path_from_outbox():
    """Return path + query for the most recent magic-link email body."""
    body = mail.outbox[-1].body.strip()
    parsed = urlparse(body)
    if parsed.query:
        return f"{parsed.path}?{parsed.query}"
    return parsed.path


def advance_to_active(project):
    """Move a draft project to active through ledger services."""
    project.refresh_from_db()
    if project.status == Project.Status.DRAFT:
        submit_criteria_for_approval(project)
    if project.status == Project.Status.CRITERIA_PENDING:
        approve_criteria(project)
    project.refresh_from_db()
    return project


def mark_all_items_passed(project):
    """Mark every acceptance item on the project as passed."""
    project.acceptance_items.update(is_passed=True)


def advance_to_delivered(project):
    """Walk a project through services until it is delivered."""
    advance_to_active(project)
    mark_all_items_passed(project)
    mark_delivered(project)
    project.refresh_from_db()
    return project


def sign_project_via_service(project, *, client_name="Client Signer"):
    """Sign a delivered project through ledger.services.sign_attestation."""
    if project.status != Project.Status.DELIVERED:
        advance_to_delivered(project)
    mark_all_items_passed(project)
    attestation = sign_attestation(
        project,
        project.client_email,
        client_name,
        {"user_agent": "Test/1.0", "ip_hash": "abc123def456"},
    )
    project.refresh_from_db()
    return attestation


def change_order_form_data(**overrides):
    """Return valid POST data for ChangeOrderForm."""
    data = {
        "description": "Additional reporting module",
        "amount_cents": 15000,
        "timeline_days": 5,
    }
    data.update(overrides)
    return data


def delivery_form_data(**overrides):
    """Return valid POST data for DeliveryItemForm."""
    data = {
        "is_passed": "True",
        "evidence_url": "https://example.com/evidence",
    }
    data.update(overrides)
    return data


def criteria_panel_html(response):
    """Return the criteria panel section markup from a detail response."""
    content = response.content.decode()
    start = content.index('id="criteria-panel"')
    end = content.index("</section>", start)
    return content[start:end]


def criterion_html(response, item):
    """Return one criterion article from a project detail response."""
    panel = criteria_panel_html(response)
    start = panel.index(f'id="criterion-{item.pk}"')
    end = panel.index("</article>", start)
    return panel[start:end]


def review_fingerprint(client, token):
    """Render a client review and return its submitted-batch fingerprint."""
    response = client.get(
        reverse("surface:client-review", kwargs={"token": token})
    )
    return response.context["approval_form"]["batch_fingerprint"].value()


def ai_dump_form_data(**overrides):
    """Return valid POST data for AiDumpForm."""
    data = {
        "source_dump": (
            "We need a reporting dashboard for Q2.\n"
            "- Export CSV\n"
            "- Filter by date range"
        ),
    }
    data.update(overrides)
    return data


def ai_confirm_form_data(**overrides):
    """Return valid POST data for AiDraftConfirmForm."""
    data = {
        "brief": "Confirmed AI brief",
        "criteria_text": "Applied criterion A\nApplied criterion B",
    }
    data.update(overrides)
    return data


class HomeViewTests(TestCase):
    """Marketing landing for anonymous visitors and project redirect when signed in."""

    def test_anonymous_home_shows_landing_page(self):
        """Anonymous GET / returns the landing page with primary marketing copy."""
        response = Client().get(reverse("surface:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Signed proof you shipped")
        self.assertContains(response, "Close your next project")

    def test_authenticated_home_redirects_to_project_list(self):
        """Signed-in freelancers are sent straight to their project list."""
        owner = make_profile()
        client = Client()
        login_as(client, owner)
        response = client.get(reverse("surface:home"))
        self.assertRedirects(response, reverse("surface:project-list"))


class MagicLinkTests(TestCase):
    """Passwordless freelancer login via signed email links."""

    def setUp(self):
        """Clear process-local auth counters and consumed-token markers."""
        cache.clear()

    def test_request_page_states_one_hour_expiry(self):
        """The request page describes the configured 60-minute lifetime."""
        response = Client().get(reverse("surface:login-request"))
        self.assertContains(response, "expires in 60 minutes")

    @override_settings(**LOCMem_EMAIL)
    def test_request_email_then_valid_token_establishes_session(self):
        """Requesting and confirming a link logs the freelancer in."""
        client = Client()
        email = "freelancer@example.com"

        response = request_magic_link(client, email)
        self.assertRedirects(response, reverse("surface:login-sent"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/login/confirm/", mail.outbox[0].body)
        self.assertIn("token=", mail.outbox[0].body)

        login_path = login_path_from_outbox()
        response = client.get(login_path)
        self.assertContains(response, "Confirm your login")
        token = response.context["token"]
        response = client.post(reverse("surface:magic-login"), {"token": token})
        self.assertRedirects(response, reverse("surface:project-list"))

        session = client.session
        user = get_user_model().objects.get(email=email)
        self.assertEqual(int(session["_auth_user_id"]), user.pk)

    @override_settings(DEBUG=True)
    def test_wrapped_token_whitespace_still_logs_in(self):
        """DEBUG repairs wrapped token whitespace through confirmation."""
        user = make_profile().user
        token = make_magic_login_token(user)
        wrapped = token[:12] + "\n " + token[12:]
        client = Client()
        response = client.get(
            reverse("surface:magic-login"),
            {"token": wrapped},
        )
        self.assertContains(response, "Confirm your login")
        response = client.post(reverse("surface:magic-login"), {"token": wrapped})
        self.assertRedirects(response, reverse("surface:project-list"))
        self.assertEqual(int(client.session["_auth_user_id"]), user.pk)

    @override_settings(DEBUG=True)
    def test_quoted_printable_console_copy_still_logs_in(self):
        """DEBUG repairs console MIME corruption through confirmation."""
        user = make_profile().user
        token = make_magic_login_token(user)
        # Matches the corruption seen in runserver console output.
        mangled = "3D" + token[:20] + "=" + token[20:]
        client = Client()
        response = client.get(
            reverse("surface:magic-login"),
            {"token": mangled},
        )
        self.assertContains(response, "Confirm your login")
        response = client.post(reverse("surface:magic-login"), {"token": mangled})
        self.assertRedirects(response, reverse("surface:project-list"))
        self.assertEqual(int(client.session["_auth_user_id"]), user.pk)

    @override_settings(DEBUG=False)
    def test_quoted_printable_token_is_rejected_outside_debug(self):
        """Production validation rejects console-only token repair."""
        user = make_profile().user
        token = make_magic_login_token(user)
        mangled = "3D" + token[:20] + "=" + token[20:]
        client = Client()
        response = client.get(
            reverse("surface:magic-login"),
            {"token": mangled},
        )
        self.assertContains(response, "This login link is no longer available")
        self.assertNotIn("_auth_user_id", client.session)

    def test_valid_token_get_does_not_consume_before_post(self):
        """Scanner-like GET validation leaves the token usable for confirmation."""
        user = make_profile().user
        token = make_magic_login_token(user)
        login_url = reverse("surface:magic-login")
        client = Client()

        get_response = client.get(login_url, {"token": token})
        self.assertContains(get_response, "Confirm your login")
        self.assertNotIn("_auth_user_id", client.session)

        post_response = client.post(login_url, {"token": token})
        self.assertRedirects(post_response, reverse("surface:project-list"))
        self.assertEqual(int(client.session["_auth_user_id"]), user.pk)

    def test_magic_login_token_first_post_succeeds_second_post_fails(self):
        """Only the first POST with a token can authenticate a session."""
        user = make_profile().user
        token = make_magic_login_token(user)
        login_url = reverse("surface:magic-login")

        first_client = Client()
        first_response = first_client.post(login_url, {"token": token})
        self.assertRedirects(first_response, reverse("surface:project-list"))

        fresh_client = Client()
        second_response = fresh_client.post(login_url, {"token": token})
        self.assertContains(second_response, "This login link is no longer available")
        self.assertNotIn("_auth_user_id", fresh_client.session)

    @override_settings(**LOCMem_EMAIL)
    def test_new_link_after_consumption_logs_in(self):
        """Requesting a fresh token after login produces another usable link."""
        email = "repeat-login@example.com"
        first_client = Client()
        request_magic_link(first_client, email)
        first_login_path = login_path_from_outbox()
        first_confirm = first_client.get(first_login_path)
        first_response = first_client.post(
            reverse("surface:magic-login"),
            {"token": first_confirm.context["token"]},
        )
        self.assertRedirects(first_response, reverse("surface:project-list"))

        request_client = Client()
        request_magic_link(request_client, email)
        second_login_path = login_path_from_outbox()
        self.assertNotEqual(second_login_path, first_login_path)

        fresh_client = Client()
        second_confirm = fresh_client.get(second_login_path)
        second_response = fresh_client.post(
            reverse("surface:magic-login"),
            {"token": second_confirm.context["token"]},
        )
        self.assertRedirects(second_response, reverse("surface:project-list"))
        self.assertIn("_auth_user_id", fresh_client.session)

    @override_settings(**LOCMem_EMAIL)
    def test_sixth_request_is_silently_limited_per_email(self):
        """The sixth request sends nothing while another email remains allowed."""
        client = Client()
        for _request_number in range(6):
            response = request_magic_link(client, "limited@example.com")
            self.assertRedirects(response, reverse("surface:login-sent"))
        self.assertEqual(len(mail.outbox), 5)

        response = request_magic_link(client, "other@example.com")
        self.assertRedirects(response, reverse("surface:login-sent"))
        self.assertEqual(len(mail.outbox), 6)
        self.assertEqual(mail.outbox[-1].to, ["other@example.com"])

    @override_settings(**LOCMem_EMAIL)
    def test_limited_request_does_not_create_unknown_user(self):
        """A rate-limited unknown email is not persisted or emailed."""
        from surface.views import _magic_link_request_allowed

        email = "stranger@example.com"
        for _ in range(5):
            _magic_link_request_allowed(email)

        response = request_magic_link(Client(), email)

        self.assertRedirects(response, reverse("surface:login-sent"))
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(get_user_model().objects.filter(email=email).exists())

    @override_settings(
        DEBUG=True,
        EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend",
    )
    def test_login_sent_shows_dev_continue_button_with_console_email(self):
        """DEBUG + console email exposes a one-click login URL on the sent page."""
        client = Client()
        response = client.post(
            reverse("surface:login-request"),
            {"email": "devclick@example.com"},
            follow=True,
        )
        self.assertContains(response, "Continue to Attest")
        self.assertContains(response, "/login/confirm/?token=")

    def test_expired_magic_token_shows_link_error_without_session(self):
        """An expired token renders link_error and does not authenticate."""
        user = make_profile().user
        base_time = 1_700_000_000
        with patch("django.core.signing.time.time", return_value=base_time):
            token = make_magic_login_token(user)
        client = Client()
        with patch(
            "django.core.signing.time.time",
            return_value=base_time + MAGIC_LOGIN_MAX_AGE + 1,
        ):
            response = client.get(
                reverse("surface:magic-login"),
                {"token": token},
            )
        self.assertContains(response, "This login link is no longer available")
        self.assertNotIn("_auth_user_id", client.session)

    def test_expired_magic_token_post_shows_link_error_without_session(self):
        """An expired token POST renders link_error and does not authenticate."""
        user = make_profile().user
        base_time = 1_700_000_000
        with patch("django.core.signing.time.time", return_value=base_time):
            token = make_magic_login_token(user)
        client = Client()
        with patch(
            "django.core.signing.time.time",
            return_value=base_time + MAGIC_LOGIN_MAX_AGE + 1,
        ):
            response = client.post(
                reverse("surface:magic-login"),
                {"token": token},
            )
        self.assertContains(response, "This login link is no longer available")
        self.assertNotIn("_auth_user_id", client.session)

    def test_tampered_magic_token_shows_link_error_without_session(self):
        """A modified token renders link_error and does not authenticate."""
        user = make_profile().user
        token = make_magic_login_token(user)
        tampered = token[:-5] + ("x" if token[-5] != "x" else "y") + token[-4:]
        client = Client()
        response = client.get(
            reverse("surface:magic-login"),
            {"token": tampered},
        )
        self.assertContains(response, "This login link is no longer available")
        self.assertNotIn("_auth_user_id", client.session)

    def test_malformed_magic_token_shows_link_error_without_session(self):
        """Garbage tokens render link_error and do not authenticate."""
        client = Client()
        response = client.get(
            reverse("surface:magic-login"),
            {"token": "not-a-valid-token"},
        )
        self.assertContains(response, "This login link is no longer available")
        self.assertNotIn("_auth_user_id", client.session)

    def test_missing_magic_token_shows_link_error_without_session(self):
        """Confirm URL without a token query param does not authenticate."""
        client = Client()
        response = client.get(reverse("surface:magic-login"))
        self.assertContains(response, "This login link is no longer available")
        self.assertNotIn("_auth_user_id", client.session)

    def test_post_missing_or_garbage_token_shows_link_error_without_session(self):
        """POST confirmation rejects absent and malformed tokens."""
        login_url = reverse("surface:magic-login")
        for post_data in ({}, {"token": "not-a-valid-token"}):
            with self.subTest(post_data=post_data):
                client = Client()
                response = client.post(login_url, post_data)
                self.assertContains(
                    response,
                    "This login link is no longer available",
                )
                self.assertNotIn("_auth_user_id", client.session)

    def test_signed_token_without_nonce_is_rejected(self):
        """A correctly signed legacy payload without a nonce cannot log in."""
        user = make_profile().user
        token = signing.TimestampSigner(salt=MAGIC_LOGIN_SALT).sign(str(user.pk))
        client = Client()

        response = client.get(reverse("surface:magic-login"), {"token": token})

        self.assertContains(response, "This login link is no longer available")
        self.assertNotIn("_auth_user_id", client.session)


class SharedCacheAuthTests(TestCase):
    """Magic-link guarantees must survive across workers sharing only the database."""

    def setUp(self):
        """Clear auth counters and consumed-token markers."""
        cache.clear()

    def other_worker_cache(self):
        """Return a cache client that shares nothing but the database table."""
        return DatabaseCache(settings.CACHE_TABLE_NAME, {})

    def test_consumed_login_marker_is_visible_to_another_worker(self):
        """A token consumed on one worker is already spent for every other worker."""
        from surface.auth import _magic_login_cache_key

        token = make_magic_login_token(make_profile().user)
        read_and_consume_magic_login_token(token)

        marker = self.other_worker_cache().get(_magic_login_cache_key(token))
        self.assertTrue(marker)

    def test_rate_limit_counter_is_visible_to_another_worker(self):
        """Per-email request counts accumulate globally rather than per process."""
        from surface.views import _magic_link_request_allowed

        email = "shared-limit@example.com"
        _magic_link_request_allowed(email)

        email_digest = sha256(email.encode("utf-8")).hexdigest()
        counter = self.other_worker_cache().get(
            f"surface.magic-login.requests:{email_digest}"
        )
        self.assertEqual(counter, 1)


class ClientTokenTests(TestCase):
    """Signed client review tokens and token_error handling."""

    def test_make_and_read_review_token_resolves_project(self):
        """A review token round-trips to the correct project."""
        project = make_draft_project()
        token = make_client_token(project, "review")
        resolved = read_client_token(token, "review")
        self.assertEqual(resolved.pk, project.pk)

    def test_expired_client_token_returns_token_error_page(self):
        """An expired review token yields HTTP 410 on the client review page."""
        project = make_draft_project()
        base_time = 1_700_000_000
        with patch("django.core.signing.time.time", return_value=base_time):
            token = make_client_token(project, "review")
        client = Client()
        with patch(
            "django.core.signing.time.time",
            return_value=base_time + CLIENT_TOKEN_MAX_AGE + 1,
        ):
            response = client.get(
                reverse("surface:client-review", kwargs={"token": token})
            )
        self.assertEqual(response.status_code, 410)
        self.assertContains(
            response,
            "This review link is no longer available",
            status_code=410,
        )

    def test_bad_client_token_returns_token_error_page(self):
        """An invalid signature yields HTTP 410 without leaking token details."""
        client = Client()
        response = client.get(
            reverse("surface:client-review", kwargs={"token": "garbage-token"})
        )
        self.assertEqual(response.status_code, 410)
        self.assertContains(
            response,
            "This review link is no longer available",
            status_code=410,
        )
        self.assertNotContains(response, "garbage-token", status_code=410)

    def test_wrong_purpose_token_returns_token_error_page(self):
        """A token signed for another purpose cannot open client review."""
        project = make_draft_project()
        wrong_purpose_token = signing.dumps(
            {"project_id": project.pk},
            salt="surface.client.other-purpose",
            compress=True,
        )
        client = Client()
        response = client.get(
            reverse(
                "surface:client-review",
                kwargs={"token": wrong_purpose_token},
            )
        )
        self.assertEqual(response.status_code, 410)
        self.assertContains(
            response,
            "This review link is no longer available",
            status_code=410,
        )

    def test_client_token_purpose_isolation_on_read(self):
        """read_client_token rejects unsupported purposes at the helper layer."""
        project = make_draft_project()
        token = make_client_token(project, "review")
        with self.assertRaises(ValueError):
            read_client_token(token, "signing")


class ProjectCrudTests(TestCase):
    """Freelancer project ownership and draft-only mutation rules."""

    def test_owner_can_create_project(self):
        """Authenticated owner can create a draft project via the form."""
        owner = make_profile()
        client = Client()
        login_as(client, owner)
        response = client.post(
            reverse("surface:project-create"),
            project_form_data(title="Created Via Form"),
        )
        project = Project.objects.get(title="Created Via Form")
        self.assertEqual(project.owner, owner)
        self.assertEqual(project.status, Project.Status.DRAFT)
        self.assertRedirects(
            response,
            reverse("surface:project-detail", kwargs={"project_pk": project.pk}),
        )

    def test_other_user_cannot_view_project_detail(self):
        """Non-owners receive 404 on another freelancer's project detail."""
        owner = make_profile(handle="owner")
        other = make_profile(handle="other")
        project = make_draft_project(owner=owner)
        client = Client()
        login_as(client, other)
        response = client.get(
            reverse("surface:project-detail", kwargs={"project_pk": project.pk})
        )
        self.assertEqual(response.status_code, 404)

    def test_other_user_cannot_edit_project(self):
        """Non-owners receive 404 when attempting draft edits."""
        owner = make_profile(handle="owner-a")
        other = make_profile(handle="other-a")
        project = make_draft_project(owner=owner)
        client = Client()
        login_as(client, other)
        response = client.post(
            reverse("surface:project-update", kwargs={"project_pk": project.pk}),
            project_form_data(title="Stolen Edit"),
        )
        self.assertEqual(response.status_code, 404)
        project.refresh_from_db()
        self.assertEqual(project.title, "Surface Test Project")

    def test_other_user_cannot_delete_project(self):
        """Non-owners receive 404 when attempting draft deletion."""
        owner = make_profile(handle="owner-b")
        other = make_profile(handle="other-b")
        project = make_draft_project(owner=owner)
        client = Client()
        login_as(client, other)
        client.get(reverse("surface:project-detail", kwargs={"project_pk": project.pk}))
        response = client.post(
            reverse("surface:project-delete", kwargs={"project_pk": project.pk})
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Project.objects.filter(pk=project.pk).exists())

    def test_owner_can_edit_draft_project(self):
        """Owners may update project details while status is draft."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        client = Client()
        login_as(client, owner)
        response = client.post(
            reverse("surface:project-update", kwargs={"project_pk": project.pk}),
            project_form_data(title="Updated Title"),
        )
        project.refresh_from_db()
        self.assertEqual(project.title, "Updated Title")
        self.assertRedirects(
            response,
            reverse("surface:project-detail", kwargs={"project_pk": project.pk}),
        )

    def test_non_draft_edit_returns_404(self):
        """Edit view is unavailable once the project leaves draft."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        submit_criteria_for_approval(project)
        client = Client()
        login_as(client, owner)
        response = client.post(
            reverse("surface:project-update", kwargs={"project_pk": project.pk}),
            project_form_data(title="Too Late"),
        )
        self.assertEqual(response.status_code, 404)
        project.refresh_from_db()
        self.assertEqual(project.title, "Surface Test Project")

    def test_owner_can_delete_draft_project(self):
        """Owners may delete a draft project with an explicit POST."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        client = Client()
        login_as(client, owner)
        client.get(reverse("surface:project-detail", kwargs={"project_pk": project.pk}))
        response = client.post(
            reverse("surface:project-delete", kwargs={"project_pk": project.pk})
        )
        self.assertRedirects(response, reverse("surface:project-list"))
        self.assertFalse(Project.objects.filter(pk=project.pk).exists())

    def test_non_draft_delete_returns_404(self):
        """Delete is unavailable once the project leaves draft."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        submit_criteria_for_approval(project)
        client = Client()
        login_as(client, owner)
        client.get(reverse("surface:project-detail", kwargs={"project_pk": project.pk}))
        response = client.post(
            reverse("surface:project-delete", kwargs={"project_pk": project.pk})
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Project.objects.filter(pk=project.pk).exists())


class CriteriaMutationTests(TestCase):
    """Acceptance criteria CRUD while editable and locked after approval."""

    def setUp(self):
        """One draft project with a single criterion for mutation tests."""
        self.owner = make_profile()
        self.project = make_draft_project(owner=self.owner)
        self.item = self.project.acceptance_items.get()
        self.client = Client()
        login_as(self.client, self.owner)

    def test_create_criterion_while_draft(self):
        """Owners can add criteria to an editable draft project."""
        response = self.client.post(
            reverse(
                "surface:criterion-create",
                kwargs={"project_pk": self.project.pk},
            ),
            {"text": "Second criterion", "order": 2},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            self.project.acceptance_items.filter(text="Second criterion").exists()
        )

    def test_update_criterion_while_draft(self):
        """Owners can edit criteria on an editable draft project."""
        response = self.client.post(
            reverse(
                "surface:criterion-update",
                kwargs={"project_pk": self.project.pk, "item_pk": self.item.pk},
            ),
            {"text": "Revised criterion", "order": 1},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.item.refresh_from_db()
        self.assertEqual(self.item.text, "Revised criterion")

    def test_delete_criterion_while_draft(self):
        """Owners can delete criteria on an editable draft project."""
        self.client.get(
            reverse("surface:project-detail", kwargs={"project_pk": self.project.pk})
        )
        response = self.client.post(
            reverse(
                "surface:criterion-delete",
                kwargs={"project_pk": self.project.pk, "item_pk": self.item.pk},
            ),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AcceptanceItem.objects.filter(pk=self.item.pk).exists())

    def test_approved_item_is_locked_but_active_project_accepts_new_draft(self):
        """Approved items stay immutable while active scope can gain a draft item."""
        submit_criteria_for_approval(self.project)
        approve_criteria(self.project)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.ACTIVE)

        create_url = reverse(
            "surface:criterion-create",
            kwargs={"project_pk": self.project.pk},
        )
        update_url = reverse(
            "surface:criterion-update",
            kwargs={"project_pk": self.project.pk, "item_pk": self.item.pk},
        )
        delete_url = reverse(
            "surface:criterion-delete",
            kwargs={"project_pk": self.project.pk, "item_pk": self.item.pk},
        )
        self.client.get(
            reverse("surface:project-detail", kwargs={"project_pk": self.project.pk})
        )

        self.assertEqual(
            self.client.post(create_url, {"text": "New scope", "order": 2}).status_code,
            302,
        )
        self.assertTrue(
            self.project.acceptance_items.filter(
                text="New scope",
                state=AcceptanceItem.State.DRAFT,
            ).exists()
        )
        self.assertEqual(
            self.client.post(
                update_url,
                {"text": "Blocked", "order": 1},
            ).status_code,
            404,
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.text, "First criterion")
        self.assertEqual(self.client.post(delete_url).status_code, 404)
        self.assertTrue(AcceptanceItem.objects.filter(pk=self.item.pk).exists())

    def test_duplicate_order_shows_error_for_htmx_create(self):
        """Duplicate order on HTMX create returns a swappable fragment with the error."""
        response = self.client.post(
            reverse(
                "surface:criterion-create",
                kwargs={"project_pk": self.project.pk},
            ),
            {"text": "Duplicate slot", "order": 1},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "That order is already in use.")
        self.assertContains(response, 'id="criteria-panel"')
        self.assertFalse(
            self.project.acceptance_items.filter(text="Duplicate slot").exists()
        )

    def test_duplicate_order_shows_error_for_non_htmx_create(self):
        """Duplicate order on a full-page create shows the error on the detail page."""
        response = self.client.post(
            reverse(
                "surface:criterion-create",
                kwargs={"project_pk": self.project.pk},
            ),
            {"text": "Duplicate slot", "order": 1},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "That order is already in use.")
        self.assertContains(response, "Project brief")
        self.assertFalse(
            self.project.acceptance_items.filter(text="Duplicate slot").exists()
        )

    def test_create_validation_error_htmx_returns_swappable_fragment(self):
        """HTMX create validation failures return status 200 with a criteria fragment."""
        response = self.client.post(
            reverse(
                "surface:criterion-create",
                kwargs={"project_pk": self.project.pk},
            ),
            {"text": "", "order": 2},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="criteria-panel"')
        self.assertContains(response, "This field is required")
        self.assertNotContains(response, "Project brief")

    def test_create_validation_error_non_htmx_returns_full_page(self):
        """Non-HTMX create validation failures render the full project detail page."""
        response = self.client.post(
            reverse(
                "surface:criterion-create",
                kwargs={"project_pk": self.project.pk},
            ),
            {"text": "", "order": 2},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Project brief")
        self.assertContains(response, "This field is required")

    def test_update_validation_error_htmx_returns_swappable_fragment(self):
        """HTMX update validation failures return status 200 with the inline form."""
        response = self.client.post(
            reverse(
                "surface:criterion-update",
                kwargs={"project_pk": self.project.pk, "item_pk": self.item.pk},
            ),
            {"text": "", "order": 1},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required")
        self.assertNotContains(response, "Project brief")

    def test_update_validation_error_non_htmx_returns_full_page(self):
        """Non-HTMX update validation failures render the full project detail page."""
        response = self.client.post(
            reverse(
                "surface:criterion-update",
                kwargs={"project_pk": self.project.pk, "item_pk": self.item.pk},
            ),
            {"text": "", "order": 1},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Project brief")
        self.assertContains(response, "This field is required")


class SubmitCriteriaViewTests(TestCase):
    """Sending draft criteria for client approval."""

    @override_settings(DEBUG=True, **LOCMem_EMAIL)
    def test_submit_sends_email_with_review_url_and_transitions_status(self):
        """Submit emails a review link and moves the project to criteria_pending."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        client = Client()
        login_as(client, owner)
        client.get(reverse("surface:project-detail", kwargs={"project_pk": project.pk}))

        response = client.post(
            reverse("surface:criteria-submit", kwargs={"project_pk": project.pk})
        )

        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.CRITERIA_PENDING)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/client/review/", mail.outbox[0].body)
        self.assertContains(response, "Criteria sent for client approval")

    @override_settings(**LOCMem_EMAIL)
    def test_review_email_keeps_action_first_and_discovers_portal(self):
        """The actual criteria-send view includes both action and portal URLs."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        client = Client()
        login_as(client, owner)

        client.post(
            reverse("surface:criteria-submit", kwargs={"project_pk": project.pk})
        )

        body_lines = [
            line for line in mail.outbox[0].body.splitlines() if line.strip()
        ]
        self.assertIn("/client/review/", body_lines[0])
        self.assertTrue(
            any("/portal/login/" in line for line in body_lines[1:]),
            "Review email did not include portal discovery after its action URL.",
        )

    @override_settings(**LOCMem_EMAIL)
    def test_submit_rejects_empty_criteria(self):
        """Submit without criteria shows an error and leaves the project draft."""
        owner = make_profile()
        project = make_draft_project(owner=owner, with_criteria=False)
        client = Client()
        login_as(client, owner)
        client.get(reverse("surface:project-detail", kwargs={"project_pk": project.pk}))

        response = client.post(
            reverse("surface:criteria-submit", kwargs={"project_pk": project.pk})
        )

        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.DRAFT)
        self.assertEqual(len(mail.outbox), 0)
        self.assertRedirects(
            response,
            reverse("surface:project-detail", kwargs={"project_pk": project.pk}),
            fetch_redirect_response=False,
        )
        follow = client.get(response.url)
        self.assertContains(
            follow,
            "Add at least one acceptance criterion first.",
        )

    @override_settings(**LOCMem_EMAIL)
    def test_submit_handles_invalid_transition(self):
        """Re-submitting after criteria_pending shows InvalidTransition messaging."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        submit_criteria_for_approval(project)
        client = Client()
        login_as(client, owner)
        client.get(reverse("surface:project-detail", kwargs={"project_pk": project.pk}))

        response = client.post(
            reverse("surface:criteria-submit", kwargs={"project_pk": project.pk})
        )

        self.assertEqual(len(mail.outbox), 0)
        self.assertRedirects(
            response,
            reverse("surface:project-detail", kwargs={"project_pk": project.pk}),
            fetch_redirect_response=False,
        )
        follow = client.get(response.url)
        self.assertContains(
            follow,
            "This project cannot be sent for approval now.",
        )

    @override_settings(DEBUG=True, **LOCMem_EMAIL)
    def test_submit_response_never_includes_review_url_in_html(self):
        """Review URLs and tokens must not appear in the HTML response, even in DEBUG."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        client = Client()
        login_as(client, owner)
        client.get(reverse("surface:project-detail", kwargs={"project_pk": project.pk}))

        response = client.post(
            reverse("surface:criteria-submit", kwargs={"project_pk": project.pk})
        )

        review_path_fragment = "/client/review/"
        self.assertNotContains(response, review_path_fragment)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(review_path_fragment, mail.outbox[0].body)


class ClientApproveViewTests(TestCase):
    """Client approval of submitted criteria via signed review tokens."""

    def setUp(self):
        """Draft project with criteria already sent for approval."""
        self.project = make_draft_project()
        submit_criteria_for_approval(self.project)
        self.project.refresh_from_db()
        self.token = make_client_token(self.project, "review")
        self.client = Client()

    def test_submitted_batch_fingerprint_hashes_complete_step_content(self):
        """The digest shape includes ordered step identity, text, order, and state."""
        from surface.views import _submitted_batch_fingerprint

        item = self.project.acceptance_items.get()
        step = AcceptanceStep.objects.create(
            item=item,
            text="Consent-bound step",
            order=7,
            is_done=True,
        )
        first_digest = _submitted_batch_fingerprint([item])
        serialized_entries = json.dumps(
            [
                [
                    item.pk,
                    item.submitted_at.isoformat(),
                    [[step.pk, step.text, step.order, step.is_done]],
                ]
            ],
            separators=(",", ":"),
        )

        self.assertEqual(
            first_digest,
            sha256(serialized_entries.encode("utf-8")).hexdigest(),
        )
        self.assertRegex(first_digest, r"\A[0-9a-f]{64}\Z")

        AcceptanceStep.objects.filter(pk=step.pk).update(
            text="Changed consent-bound step"
        )
        text_digest = _submitted_batch_fingerprint([item])
        AcceptanceStep.objects.filter(pk=step.pk).update(
            text=step.text,
            is_done=False,
        )
        done_digest = _submitted_batch_fingerprint([item])
        AcceptanceStep.objects.filter(pk=step.pk).update(
            is_done=step.is_done,
            order=8,
        )
        order_digest = _submitted_batch_fingerprint([item])

        self.assertNotEqual(first_digest, text_digest)
        self.assertNotEqual(first_digest, done_digest)
        self.assertNotEqual(first_digest, order_digest)

    def test_submitted_batch_fingerprint_sorts_reversed_item_input(self):
        """Fingerprint order is deterministic even when input arrives newest-first."""
        from surface.views import _submitted_batch_fingerprint

        first_item = self.project.acceptance_items.get()
        second_item = AcceptanceItem.objects.create(
            project=self.project,
            text="Second submitted criterion",
            order=2,
            state=AcceptanceItem.State.SUBMITTED,
            submitted_at=timezone.now(),
        )
        reversed_items = self.project.acceptance_items.filter(
            state=AcceptanceItem.State.SUBMITTED
        ).order_by("-pk")
        serialized_entries = json.dumps(
            [
                [first_item.pk, first_item.submitted_at.isoformat(), []],
                [second_item.pk, second_item.submitted_at.isoformat(), []],
            ],
            separators=(",", ":"),
        )

        self.assertEqual(
            _submitted_batch_fingerprint(reversed_items),
            sha256(serialized_entries.encode("utf-8")).hexdigest(),
        )

    def test_submitted_batch_fingerprint_pins_null_timestamp_sentinel(self):
        """A missing timestamp has one explicit consent-bound representation."""
        from surface.views import _submitted_batch_fingerprint

        item = self.project.acceptance_items.get()
        item.submitted_at = None
        item.save(update_fields=("submitted_at",))
        serialized_entries = json.dumps(
            [[item.pk, "__missing_submitted_at__", []]],
            separators=(",", ":"),
        )

        self.assertEqual(
            _submitted_batch_fingerprint([item]),
            sha256(serialized_entries.encode("utf-8")).hexdigest(),
        )

    def test_approve_fingerprint_prefetches_steps_with_locked_items(self):
        """The real approval path fetches all locked items and steps without N+1."""
        from surface.views import _submitted_batch_fingerprint

        second_item = AcceptanceItem.objects.create(
            project=self.project,
            text="Second submitted criterion",
            order=2,
            state=AcceptanceItem.State.SUBMITTED,
            submitted_at=timezone.now(),
        )
        for item in (self.project.acceptance_items.get(order=1), second_item):
            AcceptanceStep.objects.create(
                item=item,
                text=f"Step for item {item.pk}",
                order=1,
            )

        submitted_items = list(
            self.project.acceptance_items.filter(
                state=AcceptanceItem.State.SUBMITTED
            )
        )
        fingerprint = _submitted_batch_fingerprint(submitted_items)
        with (
            patch("surface.views.services.approve_acceptance_items"),
            patch("surface.views.services.approve_criteria"),
            CaptureQueriesContext(connection) as captured_queries,
        ):
            response = self.client.post(
                reverse(
                    "surface:client-approve",
                    kwargs={"token": self.token},
                ),
                {"batch_fingerprint": fingerprint},
            )

        step_selects = [
            query["sql"]
            for query in captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "ledger_acceptancestep" in query["sql"].lower()
        ]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(captured_queries), 7)
        self.assertEqual(len(step_selects), 1)

    def test_client_review_shows_step_scope_without_progress_state(self):
        """Review discloses step text while withholding irrelevant done status."""
        item = self.project.acceptance_items.get()
        AcceptanceStep.objects.create(
            item=item,
            text="Client-visible review step",
            order=1,
            is_done=True,
        )

        response = self.client.get(
            reverse("surface:client-review", kwargs={"token": self.token})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Client-visible review step")
        self.assertNotContains(response, 'class="badge passed"')
        self.assertNotContains(response, 'class="badge failed"')

    def test_client_review_omits_step_block_for_empty_scope(self):
        """A criterion without steps gets no empty nested-list placeholder."""
        response = self.client.get(
            reverse("surface:client-review", kwargs={"token": self.token})
        )

        self.assertNotContains(response, 'class="step-list"')

    def test_client_approve_activates_project(self):
        """Valid approve POST activates the project through ledger.services."""
        fingerprint = review_fingerprint(self.client, self.token)
        response = self.client.post(
            reverse("surface:client-approve", kwargs={"token": self.token}),
            {"batch_fingerprint": fingerprint},
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.ACTIVE)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Thank you")

    def test_client_approve_invalid_token_returns_410(self):
        """Invalid approve tokens render token_error with HTTP 410."""
        response = self.client.post(
            reverse("surface:client-approve", kwargs={"token": "not-valid"})
        )
        self.assertEqual(response.status_code, 410)
        self.assertContains(
            response,
            "This review link is no longer available",
            status_code=410,
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.CRITERIA_PENDING)

    def test_client_approve_empty_handled_batch_rerenders_review(self):
        """A handled batch re-renders review instead of approving empty scope."""
        approve_criteria(self.project)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.ACTIVE)

        response = self.client.post(
            reverse("surface:client-approve", kwargs={"token": self.token}),
            {"batch_fingerprint": "[]"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No criteria are currently awaiting approval.")
        self.assertContains(response, "These criteria have already been handled.")
        self.assertNotContains(response, "criteria changed after this page was shown")

    def test_client_approve_blank_fingerprint_is_stale_not_empty(self):
        """A blank consent fingerprint is stale while submitted scope still exists."""
        response = self.client.post(
            reverse("surface:client-approve", kwargs={"token": self.token}),
            {"batch_fingerprint": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "criteria changed after this page was shown")
        self.assertNotContains(response, "No criteria are currently awaiting approval.")
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.CRITERIA_PENDING)
        self.assertFalse(
            self.project.acceptance_items.filter(
                state=AcceptanceItem.State.APPROVED
            ).exists()
        )

    def test_active_project_batch_does_not_reapprove_initial_criteria(self):
        """Later submitted scope is approved without replaying initial activation."""
        approve_criteria(self.project)
        later_item = AcceptanceItem.objects.create(
            project=self.project,
            text="Later submitted criterion",
            order=2,
            state=AcceptanceItem.State.SUBMITTED,
            submitted_at=timezone.now(),
        )
        fingerprint = review_fingerprint(self.client, self.token)

        with patch("ledger.services.approve_criteria") as approve_project:
            response = self.client.post(
                reverse("surface:client-approve", kwargs={"token": self.token}),
                {"batch_fingerprint": fingerprint},
            )

        approve_project.assert_not_called()
        later_item.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Thank you")
        self.assertEqual(later_item.state, AcceptanceItem.State.APPROVED)
        self.assertEqual(self.project.status, Project.Status.ACTIVE)

    def test_approval_losing_a_race_is_not_reported_as_thanks(self):
        """A rolled-back approval re-renders review instead of confirming."""
        fingerprint = review_fingerprint(self.client, self.token)
        with patch(
            "surface.views.services.approve_acceptance_items",
            side_effect=InvalidTransition("state changed"),
        ):
            response = self.client.post(
                reverse("surface:client-approve", kwargs={"token": self.token}),
                {"batch_fingerprint": fingerprint},
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Thank you")
        self.assertContains(response, "criteria changed after this page was shown")
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.CRITERIA_PENDING)
        self.assertFalse(
            self.project.acceptance_items.filter(
                state=AcceptanceItem.State.APPROVED
            ).exists()
        )

    def test_failure_after_items_approved_commits_nothing(self):
        """A late failure rolls the whole approval back, not just its own step."""
        fingerprint = review_fingerprint(self.client, self.token)
        with patch(
            "surface.views.services.approve_criteria",
            side_effect=InvalidTransition("status changed"),
        ):
            response = self.client.post(
                reverse("surface:client-approve", kwargs={"token": self.token}),
                {"batch_fingerprint": fingerprint},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "criteria changed after this page was shown")
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.CRITERIA_PENDING)
        self.assertFalse(
            self.project.acceptance_items.filter(
                state=AcceptanceItem.State.APPROVED
            ).exists()
        )

    def test_null_submitted_at_is_stable_on_review_and_approval(self):
        """Admin-malformed submitted rows remain consent-bound without crashing."""
        item = self.project.acceptance_items.get()
        # Surface services always stamp this field. Direct persistence models the
        # independently editable admin fields that can create this malformed row.
        item.submitted_at = None
        item.save(update_fields=("submitted_at",))

        review_response = self.client.get(
            reverse("surface:client-review", kwargs={"token": self.token})
        )
        fingerprint = review_response.context["approval_form"][
            "batch_fingerprint"
        ].value()
        self.assertEqual(review_response.status_code, 200)
        self.assertContains(review_response, item.text)
        self.assertRegex(fingerprint, r"\A[0-9a-f]{64}\Z")

        approve_response = self.client.post(
            reverse("surface:client-approve", kwargs={"token": self.token}),
            {"batch_fingerprint": fingerprint},
        )
        item.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(approve_response.status_code, 200)
        self.assertEqual(item.state, AcceptanceItem.State.APPROVED)
        self.assertEqual(self.project.status, Project.Status.ACTIVE)


class PendingCriteriaWorkflowTests(TestCase):
    """Freelancer item corrections while the initial client review is pending."""

    def setUp(self):
        """Create an owned two-item project awaiting initial client approval."""
        self.owner = make_profile()
        self.project = make_draft_project(owner=self.owner)
        AcceptanceItem.objects.create(
            project=self.project,
            text="Second criterion",
            order=2,
        )
        submit_criteria_for_approval(self.project)
        self.project.refresh_from_db()
        self.client = Client()
        login_as(self.client, self.owner)

    def action_url(self, action, item):
        """Return one per-item action URL for the pending project."""
        return reverse(
            f"surface:criterion-{action}",
            kwargs={"project_pk": self.project.pk, "item_pk": item.pk},
        )

    def test_pull_back_and_edit_through_ui_while_criteria_pending(self):
        """Pending review still permits pull-back followed by an honest text edit."""
        item = self.project.acceptance_items.order_by("order").first()

        pull_back_response = self.client.post(
            self.action_url("pull-back", item),
            HTTP_HX_REQUEST="true",
        )
        item.refresh_from_db()
        self.assertEqual(pull_back_response.status_code, 200)
        self.assertEqual(item.state, AcceptanceItem.State.DRAFT)
        self.assertContains(pull_back_response, ">Edit</button>")
        self.assertContains(pull_back_response, ">Send for approval</button>")

        edit_response = self.client.post(
            reverse(
                "surface:criterion-update",
                kwargs={"project_pk": self.project.pk, "item_pk": item.pk},
            ),
            {"text": "Corrected first criterion", "order": item.order},
            HTTP_HX_REQUEST="true",
        )
        item.refresh_from_db()
        self.assertEqual(edit_response.status_code, 200)
        self.assertEqual(item.text, "Corrected first criterion")
        self.assertContains(edit_response, "Corrected first criterion")

    @override_settings(**LOCMem_EMAIL)
    def test_pull_back_every_item_can_resubmit_and_reach_active_through_ui(self):
        """Pulling back the whole pending batch cannot deadlock initial approval."""
        items = list(self.project.acceptance_items.order_by("order"))
        for item in items:
            response = self.client.post(
                self.action_url("pull-back", item),
                HTTP_HX_REQUEST="true",
            )
            self.assertEqual(response.status_code, 200)
        self.assertFalse(
            self.project.acceptance_items.filter(
                state=AcceptanceItem.State.SUBMITTED
            ).exists()
        )
        detail_response = self.client.get(
            reverse(
                "surface:project-detail",
                kwargs={"project_pk": self.project.pk},
            )
        )
        for item in items:
            self.assertIn(
                ">Send for approval</button>",
                criterion_html(detail_response, item),
            )

        for item in items:
            response = self.client.post(
                self.action_url("submit", item),
                HTTP_HX_REQUEST="true",
            )
            self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.project.acceptance_items.filter(
                state=AcceptanceItem.State.SUBMITTED
            ).count(),
            len(items),
        )

        token = make_client_token(self.project, "review")
        anonymous_client = Client()
        fingerprint = review_fingerprint(anonymous_client, token)
        approve_response = anonymous_client.post(
            reverse("surface:client-approve", kwargs={"token": token}),
            {"batch_fingerprint": fingerprint},
        )

        self.project.refresh_from_db()
        self.assertEqual(approve_response.status_code, 200)
        self.assertContains(approve_response, "Thank you")
        self.assertEqual(self.project.status, Project.Status.ACTIVE)
        self.assertFalse(
            self.project.acceptance_items.exclude(
                state=AcceptanceItem.State.APPROVED
            ).exists()
        )

    @override_settings(**LOCMem_EMAIL)
    def test_delete_every_item_can_add_replacement_and_reach_active_through_ui(self):
        """Deleting pulled-back scope cannot deadlock the pending review."""
        items = list(self.project.acceptance_items.order_by("order"))
        for item in items:
            pull_back_response = self.client.post(
                self.action_url("pull-back", item),
                HTTP_HX_REQUEST="true",
            )
            self.assertEqual(pull_back_response.status_code, 200)
            delete_response = self.client.post(
                self.action_url("delete", item),
                HTTP_HX_REQUEST="true",
            )
            self.assertEqual(delete_response.status_code, 200)
        self.assertFalse(self.project.acceptance_items.exists())

        empty_detail = self.client.get(
            reverse(
                "surface:project-detail",
                kwargs={"project_pk": self.project.pk},
            )
        )
        self.assertContains(empty_detail, ">Add criterion</button>")
        create_response = self.client.post(
            reverse(
                "surface:criterion-create",
                kwargs={"project_pk": self.project.pk},
            ),
            {"text": "Replacement criterion", "order": 1},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(create_response.status_code, 200)
        replacement = self.project.acceptance_items.get()
        self.assertEqual(replacement.state, AcceptanceItem.State.DRAFT)
        self.assertContains(create_response, "Replacement criterion")
        self.assertContains(create_response, ">Send for approval</button>")

        submit_response = self.client.post(
            self.action_url("submit", replacement),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(submit_response.status_code, 200)
        token = make_client_token(self.project, "review")
        anonymous_client = Client()
        fingerprint = review_fingerprint(anonymous_client, token)
        approve_response = anonymous_client.post(
            reverse("surface:client-approve", kwargs={"token": token}),
            {"batch_fingerprint": fingerprint},
        )

        self.project.refresh_from_db()
        replacement.refresh_from_db()
        self.assertEqual(approve_response.status_code, 200)
        self.assertEqual(self.project.status, Project.Status.ACTIVE)
        self.assertEqual(replacement.state, AcceptanceItem.State.APPROVED)


class CriteriaActionGateAgreementTests(TestCase):
    """Keep rendered criteria controls aligned with reachable endpoints."""

    @override_settings(**LOCMem_EMAIL)
    def test_controls_and_endpoints_agree_across_all_project_statuses(self):
        """Pin both bounds of all criteria controls across project statuses."""
        owner = make_profile()
        client = Client()
        login_as(client, owner)
        action_cases = {
            "submit": (AcceptanceItem.State.DRAFT, "Send for approval"),
            "pull-back": (AcceptanceItem.State.SUBMITTED, "Pull back"),
            "suspend": (AcceptanceItem.State.SUBMITTED, "Suspend"),
            "resume": (AcceptanceItem.State.SUSPENDED, "Resume"),
            "withdraw": (AcceptanceItem.State.APPROVED, "Withdraw"),
        }
        statuses = (
            Project.Status.DRAFT,
            Project.Status.CRITERIA_PENDING,
            Project.Status.ACTIVE,
            Project.Status.DELIVERED,
            Project.Status.ATTESTED,
            Project.Status.DISPUTED,
        )
        mutable_statuses = {
            Project.Status.CRITERIA_PENDING,
            Project.Status.ACTIVE,
        }
        create_statuses = {
            Project.Status.DRAFT,
            Project.Status.CRITERIA_PENDING,
            Project.Status.ACTIVE,
        }
        checks = 0

        for status in statuses:
            for action, (item_state, control_label) in action_cases.items():
                project = make_draft_project(owner=owner, with_criteria=False)
                project.status = status
                project.save(update_fields=("status",))
                timestamp = timezone.now()
                item = AcceptanceItem.objects.create(
                    project=project,
                    text=f"{status} {action} criterion",
                    order=1,
                    state=item_state,
                    submitted_at=(
                        None
                        if item_state == AcceptanceItem.State.DRAFT
                        else timestamp
                    ),
                    approved_at=(
                        timestamp
                        if item_state == AcceptanceItem.State.APPROVED
                        else None
                    ),
                    is_passed=True,
                )
                detail_response = client.get(
                    reverse(
                        "surface:project-detail",
                        kwargs={"project_pk": project.pk},
                    )
                )
                control_is_rendered = (
                    f">{control_label}</button>"
                    in criterion_html(detail_response, item)
                )
                endpoint_response = client.post(
                    reverse(
                        f"surface:criterion-{action}",
                        kwargs={"project_pk": project.pk, "item_pk": item.pk},
                    ),
                    HTTP_HX_REQUEST="true",
                )
                endpoint_is_reachable = endpoint_response.status_code != 404
                expected_reachable = status in mutable_statuses

                self.assertEqual(
                    control_is_rendered,
                    expected_reachable,
                    f"{control_label} rendering disagrees for {status}",
                )
                self.assertEqual(
                    endpoint_is_reachable,
                    expected_reachable,
                    f"{action} endpoint disagrees for {status}",
                )
                checks += 2

        for status in statuses:
            project = make_draft_project(owner=owner, with_criteria=False)
            project.status = status
            project.save(update_fields=("status",))
            detail_response = client.get(
                reverse(
                    "surface:project-detail",
                    kwargs={"project_pk": project.pk},
                )
            )
            control_is_rendered = (
                ">Add criterion</button>" in criteria_panel_html(detail_response)
            )
            endpoint_response = client.post(
                reverse(
                    "surface:criterion-create",
                    kwargs={"project_pk": project.pk},
                ),
                {"text": f"{status} replacement criterion", "order": 1},
                HTTP_HX_REQUEST="true",
            )
            endpoint_is_reachable = endpoint_response.status_code != 404
            expected_reachable = status in create_statuses

            self.assertEqual(
                control_is_rendered,
                expected_reachable,
                f"Add criterion rendering disagrees for {status}",
            )
            self.assertEqual(
                endpoint_is_reachable,
                expected_reachable,
                f"criterion-create endpoint disagrees for {status}",
            )
            checks += 2

        self.assertEqual(checks, 72)

    def test_delete_endpoint_agrees_with_scope_mutability_across_statuses(self):
        """The delete route is reachable only before project delivery."""
        owner = make_profile()
        client = Client()
        login_as(client, owner)
        mutable_statuses = {
            Project.Status.DRAFT,
            Project.Status.CRITERIA_PENDING,
            Project.Status.ACTIVE,
        }
        for status in Project.Status.values:
            with self.subTest(status=status):
                project = make_draft_project(owner=owner)
                project.status = status
                project.save(update_fields=("status",))
                item = project.acceptance_items.get()
                item.state = AcceptanceItem.State.SUSPENDED
                item.submitted_at = timezone.now()
                item.save(update_fields=("state", "submitted_at"))

                delete_url = reverse(
                    "surface:criterion-delete",
                    kwargs={"project_pk": project.pk, "item_pk": item.pk},
                )
                if status in mutable_statuses:
                    response = client.post(
                        delete_url,
                        HTTP_HX_REQUEST="true",
                    )
                else:
                    with patch(
                        "surface.views.services.delete_acceptance_item"
                    ) as delete_service:
                        response = client.post(
                            delete_url,
                            HTTP_HX_REQUEST="true",
                        )
                    delete_service.assert_not_called()

                expected_status = 200 if status in mutable_statuses else 404
                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(
                    AcceptanceItem.objects.filter(pk=item.pk).exists(),
                    status not in mutable_statuses,
                )


class AcceptanceStepSurfaceTests(TestCase):
    """Freelancer step controls and endpoints mirror Ledger's exact guards."""

    def setUp(self):
        """Authenticate one freelancer for owned-project step requests."""
        self.owner = make_profile()
        self.client = Client()
        login_as(self.client, self.owner)

    def step_url(self, action, project, item, step=None):
        """Build one nested step URL using the established route names."""
        kwargs = {"project_pk": project.pk, "item_pk": item.pk}
        if step is not None:
            kwargs["step_pk"] = step.pk
        return reverse(f"surface:criterion-step-{action}", kwargs=kwargs)

    def make_item_for_state(self, project, state):
        """Create one internally coherent item fixture in the requested state."""
        timestamp = timezone.now()
        return AcceptanceItem.objects.create(
            project=project,
            text=f"{project.status} {state} criterion",
            order=1,
            state=state,
            submitted_at=(
                None if state == AcceptanceItem.State.DRAFT else timestamp
            ),
            approved_at=(
                timestamp if state == AcceptanceItem.State.APPROVED else None
            ),
            is_passed=True,
        )

    def test_structure_controls_and_endpoints_agree_across_every_state_matrix(self):
        """Create, edit, and delete exist exactly while the parent is draft."""
        checks = 0
        for status in Project.Status.values:
            for item_state in AcceptanceItem.State.values:
                with self.subTest(status=status, item_state=item_state):
                    project = make_draft_project(
                        owner=self.owner,
                        with_criteria=False,
                    )
                    project.status = status
                    project.save(update_fields=("status",))
                    item = self.make_item_for_state(project, item_state)
                    step = AcceptanceStep.objects.create(
                        item=item,
                        text="Existing step",
                        order=1,
                    )
                    detail_response = self.client.get(
                        reverse(
                            "surface:project-detail",
                            kwargs={"project_pk": project.pk},
                        )
                    )
                    item_markup = criterion_html(detail_response, item)
                    expected_reachable = item_state == AcceptanceItem.State.DRAFT

                    for label in ("Add step", "Edit step", "Delete step"):
                        self.assertEqual(
                            f">{label}</button>" in item_markup,
                            expected_reachable,
                            f"{label} rendering disagrees for {status}/{item_state}",
                        )
                        checks += 1

                    update_get_response = self.client.get(
                        self.step_url("update", project, item, step),
                        HTTP_HX_REQUEST="true",
                    )
                    with patch(
                        "surface.views.services.create_acceptance_step"
                    ):
                        create_response = self.client.post(
                            self.step_url("create", project, item),
                            {"text": "Created step", "order": 2},
                            HTTP_HX_REQUEST="true",
                        )
                    with patch(
                        "surface.views.services.update_acceptance_step"
                    ):
                        update_response = self.client.post(
                            self.step_url("update", project, item, step),
                            {"text": "Updated step", "order": 1},
                            HTTP_HX_REQUEST="true",
                        )
                    with patch(
                        "surface.views.services.delete_acceptance_step"
                    ):
                        delete_response = self.client.post(
                            self.step_url("delete", project, item, step),
                            HTTP_HX_REQUEST="true",
                        )
                    for action, response in (
                        ("create", create_response),
                        ("update-get", update_get_response),
                        ("update", update_response),
                        ("delete", delete_response),
                    ):
                        self.assertEqual(
                            response.status_code != 404,
                            expected_reachable,
                            f"{action} endpoint disagrees for {status}/{item_state}",
                        )
                        checks += 1
        self.assertEqual(checks, 210)

    def test_draft_step_edit_get_renders_bound_inline_form(self):
        """The Edit-step HTMX endpoint returns a usable form for its selected step."""
        project = make_draft_project(owner=self.owner)
        item = project.acceptance_items.get()
        step = AcceptanceStep.objects.create(
            item=item,
            text="Editable inline step",
            order=7,
        )

        response = self.client.get(
            self.step_url("update", project, item, step),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Editable inline step")
        self.assertContains(response, 'value="7"')
        self.assertContains(response, ">Save step</button>")

    def test_done_control_and_endpoint_agree_across_every_state_matrix(self):
        """Progress is writable only for approved scope on an active project."""
        checks = 0
        for status in Project.Status.values:
            for item_state in AcceptanceItem.State.values:
                with self.subTest(status=status, item_state=item_state):
                    project = make_draft_project(
                        owner=self.owner,
                        with_criteria=False,
                    )
                    project.status = status
                    project.save(update_fields=("status",))
                    item = self.make_item_for_state(project, item_state)
                    step = AcceptanceStep.objects.create(
                        item=item,
                        text="Progress step",
                        order=1,
                    )
                    detail_response = self.client.get(
                        reverse(
                            "surface:project-detail",
                            kwargs={"project_pk": project.pk},
                        )
                    )
                    control_is_rendered = (
                        ">Mark done</button>"
                        in criterion_html(detail_response, item)
                    )
                    expected_reachable = (
                        status == Project.Status.ACTIVE
                        and item_state == AcceptanceItem.State.APPROVED
                    )
                    with patch(
                        "surface.views.services.set_acceptance_step_done"
                    ):
                        endpoint_response = self.client.post(
                            self.step_url("done", project, item, step),
                            {"is_done": "True"},
                            HTTP_HX_REQUEST="true",
                        )

                    self.assertEqual(
                        control_is_rendered,
                        expected_reachable,
                        f"done rendering disagrees for {status}/{item_state}",
                    )
                    self.assertEqual(
                        endpoint_response.status_code != 404,
                        expected_reachable,
                        f"done endpoint disagrees for {status}/{item_state}",
                    )
                    checks += 2
        self.assertEqual(checks, 60)

    def test_done_posts_explicit_idempotent_boolean_intent(self):
        """Repeated intent cannot invert progress, and either boolean is accepted."""
        project = make_draft_project(owner=self.owner)
        advance_to_active(project)
        item = project.acceptance_items.get()
        step = AcceptanceStep.objects.create(
            item=item,
            text="Explicit progress",
            order=1,
        )
        url = self.step_url("done", project, item, step)

        for _submission in range(2):
            response = self.client.post(
                url,
                {"is_done": "True"},
                HTTP_HX_REQUEST="true",
            )
            step.refresh_from_db()
            self.assertEqual(response.status_code, 200)
            self.assertTrue(step.is_done)
            self.assertContains(response, ">Mark not done</button>")

        response = self.client.post(
            url,
            {"is_done": "False"},
            HTTP_HX_REQUEST="true",
        )
        step.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(step.is_done)

    def test_owner_can_create_update_and_delete_draft_steps(self):
        """All three structure endpoints delegate real mutations while draft."""
        project = make_draft_project(owner=self.owner)
        item = project.acceptance_items.get()

        create_response = self.client.post(
            self.step_url("create", project, item),
            {"text": "Created through Surface", "order": 1},
            HTTP_HX_REQUEST="true",
        )
        step = item.steps.get()
        update_response = self.client.post(
            self.step_url("update", project, item, step),
            {"text": "Updated through Surface", "order": 1},
            HTTP_HX_REQUEST="true",
        )
        step.refresh_from_db()
        delete_response = self.client.post(
            self.step_url("delete", project, item, step),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(step.text, "Updated through Surface")
        self.assertEqual(delete_response.status_code, 200)
        self.assertFalse(AcceptanceStep.objects.filter(pk=step.pk).exists())

    def test_duplicate_step_order_returns_inline_form_error(self):
        """A duplicate position is a friendly form error, never a server error."""
        project = make_draft_project(owner=self.owner)
        item = project.acceptance_items.get()
        AcceptanceStep.objects.create(item=item, text="First step", order=1)

        response = self.client.post(
            self.step_url("create", project, item),
            {"text": "Duplicate step", "order": 1},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "That order is already in use.")
        self.assertEqual(item.steps.count(), 1)

    def test_duplicate_step_order_on_update_returns_inline_form_error(self):
        """Reordering onto a sibling position keeps the stored step unchanged."""
        project = make_draft_project(owner=self.owner)
        item = project.acceptance_items.get()
        first_step = AcceptanceStep.objects.create(
            item=item,
            text="First step",
            order=1,
        )
        second_step = AcceptanceStep.objects.create(
            item=item,
            text="Second step",
            order=2,
        )

        response = self.client.post(
            self.step_url("update", project, item, second_step),
            {"text": "Conflicting update", "order": 1},
            HTTP_HX_REQUEST="true",
        )

        second_step.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "That order is already in use.")
        self.assertEqual(second_step.text, "Second step")
        self.assertEqual(second_step.order, 2)
        self.assertTrue(AcceptanceStep.objects.filter(pk=first_step.pk).exists())

    def test_foreign_project_step_is_404_for_every_step_endpoint(self):
        """Nested lookup never resolves another freelancer's step by primary key."""
        owned_project = make_draft_project(owner=self.owner)
        other_project = make_draft_project(owner=make_profile())
        foreign_item = other_project.acceptance_items.get()
        foreign_step = AcceptanceStep.objects.create(
            item=foreign_item,
            text="Foreign step",
            order=1,
        )
        kwargs = {
            "project_pk": owned_project.pk,
            "item_pk": foreign_item.pk,
            "step_pk": foreign_step.pk,
        }

        update_url = reverse("surface:criterion-step-update", kwargs=kwargs)
        responses = [
            self.client.post(
                self.step_url("create", owned_project, foreign_item),
                {"text": "Foreign create intrusion", "order": 2},
                HTTP_HX_REQUEST="true",
            ),
            self.client.get(update_url, HTTP_HX_REQUEST="true"),
            self.client.post(
                update_url,
                {"text": "Intrusion", "order": 1},
                HTTP_HX_REQUEST="true",
            ),
            self.client.post(
                reverse("surface:criterion-step-delete", kwargs=kwargs),
                HTTP_HX_REQUEST="true",
            ),
            self.client.post(
                reverse("surface:criterion-step-done", kwargs=kwargs),
                {"is_done": "True"},
                HTTP_HX_REQUEST="true",
            ),
        ]

        self.assertTrue(all(response.status_code == 404 for response in responses))
        foreign_step.refresh_from_db()
        self.assertEqual(foreign_step.text, "Foreign step")
        self.assertFalse(foreign_step.is_done)
        self.assertEqual(foreign_item.steps.count(), 1)


class PerItemCriteriaWorkflowTests(TestCase):
    """Freelancer item controls and server-bound client consent."""

    def setUp(self):
        """Create one active owned project with an approved baseline item."""
        self.owner = make_profile()
        self.project = make_draft_project(owner=self.owner)
        advance_to_active(self.project)
        self.approved_item = self.project.acceptance_items.get()
        self.client = Client()
        login_as(self.client, self.owner)

    def add_draft_item(self, *, text="Mid-project criterion", order=2):
        """Create one draft item directly as setup for Surface transitions."""
        return AcceptanceItem.objects.create(
            project=self.project,
            text=text,
            order=order,
        )

    def action_url(self, action, item):
        """Return one per-item action URL."""
        return reverse(
            f"surface:criterion-{action}",
            kwargs={"project_pk": self.project.pk, "item_pk": item.pk},
        )

    @override_settings(**LOCMem_EMAIL)
    def test_per_item_submit_emails_working_review_link(self):
        """Active-project submit stamps the item and emails a usable review page."""
        item = self.add_draft_item()

        response = self.client.post(
            self.action_url("submit", item),
            HTTP_HX_REQUEST="true",
        )

        item.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(item.state, AcceptanceItem.State.SUBMITTED)
        self.assertIsNotNone(item.submitted_at)
        self.assertEqual(len(mail.outbox), 1)
        review_path = urlparse(mail.outbox[0].body.splitlines()[0]).path
        review_response = Client().get(review_path)
        self.assertEqual(review_response.status_code, 200)
        self.assertContains(review_response, item.text)

    @override_settings(**LOCMem_EMAIL)
    def test_per_item_submit_is_refused_before_project_is_active(self):
        """The item endpoint cannot bypass initial package review from draft."""
        draft_project = make_draft_project(owner=self.owner)
        item = draft_project.acceptance_items.get()

        response = self.client.post(
            reverse(
                "surface:criterion-submit",
                kwargs={"project_pk": draft_project.pk, "item_pk": item.pk},
            )
        )

        item.refresh_from_db()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(item.state, AcceptanceItem.State.DRAFT)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(**LOCMem_EMAIL)
    def test_pull_back_submitted_item_through_ui(self):
        """Pull-back returns submitted scope to editable draft and clears consent time."""
        item = self.add_draft_item()
        self.client.post(self.action_url("submit", item))

        response = self.client.post(
            self.action_url("pull-back", item),
            HTTP_HX_REQUEST="true",
        )

        item.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(item.state, AcceptanceItem.State.DRAFT)
        self.assertIsNone(item.submitted_at)
        self.assertContains(response, "Edit")

    def test_suspend_and_resume_approved_item_through_ui(self):
        """Suspend parks approved scope and resume restores its approved state."""
        suspend_response = self.client.post(
            self.action_url("suspend", self.approved_item),
            HTTP_HX_REQUEST="true",
        )
        self.approved_item.refresh_from_db()
        self.assertEqual(suspend_response.status_code, 200)
        self.assertEqual(self.approved_item.state, AcceptanceItem.State.SUSPENDED)
        self.assertContains(suspend_response, "Resume")

        resume_response = self.client.post(
            self.action_url("resume", self.approved_item),
            HTTP_HX_REQUEST="true",
        )
        self.approved_item.refresh_from_db()
        self.assertEqual(resume_response.status_code, 200)
        self.assertEqual(self.approved_item.state, AcceptanceItem.State.APPROVED)
        self.assertContains(resume_response, "Withdraw")

    def test_withdraw_approved_item_through_ui(self):
        """Withdraw moves approved scope to its terminal visible state."""
        response = self.client.post(
            self.action_url("withdraw", self.approved_item),
            HTTP_HX_REQUEST="true",
        )

        self.approved_item.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.approved_item.state, AcceptanceItem.State.WITHDRAWN)
        self.assertContains(response, "Withdrawn")
        self.assertNotContains(response, ">Resume<")

    def test_delete_refuses_item_that_was_client_approved(self):
        """The delete route delegates the approved-history guard to Ledger."""
        response = self.client.post(
            self.action_url("delete", self.approved_item),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(
            AcceptanceItem.objects.filter(pk=self.approved_item.pk).exists()
        )

    @override_settings(**LOCMem_EMAIL)
    def test_client_approves_current_submitted_batch_only(self):
        """Approval covers rendered submitted scope, not approved or draft items."""
        submitted_item = self.add_draft_item(text="Awaiting client")
        untouched_draft = self.add_draft_item(text="Not yet sent", order=3)
        self.client.post(self.action_url("submit", submitted_item))
        token = make_client_token(self.project, "review")
        anonymous_client = Client()
        review_response = anonymous_client.get(
            reverse("surface:client-review", kwargs={"token": token})
        )
        fingerprint = review_response.context["approval_form"][
            "batch_fingerprint"
        ].value()
        self.assertContains(review_response, "Criteria awaiting your approval")
        self.assertContains(review_response, "Already-approved scope")
        self.assertContains(review_response, submitted_item.text)
        self.assertContains(review_response, self.approved_item.text)
        self.assertNotContains(review_response, untouched_draft.text)

        response = anonymous_client.post(
            reverse("surface:client-approve", kwargs={"token": token}),
            {"batch_fingerprint": fingerprint},
        )

        submitted_item.refresh_from_db()
        untouched_draft.refresh_from_db()
        self.approved_item.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Thank you")
        self.assertEqual(submitted_item.state, AcceptanceItem.State.APPROVED)
        self.assertEqual(untouched_draft.state, AcceptanceItem.State.DRAFT)
        self.assertEqual(self.approved_item.state, AcceptanceItem.State.APPROVED)
        self.assertEqual(self.project.status, Project.Status.ACTIVE)

    @override_settings(**LOCMem_EMAIL)
    def test_crafted_item_ids_cannot_widen_server_derived_batch(self):
        """Posted ids never authorize approval beyond current submitted scope."""
        submitted_item = self.add_draft_item(text="Server-derived scope")
        same_project_draft = self.add_draft_item(text="Unshown draft", order=3)
        other_project = make_draft_project(owner=self.owner)
        other_project_item = other_project.acceptance_items.get()
        self.client.post(self.action_url("submit", submitted_item))
        token = make_client_token(self.project, "review")
        anonymous_client = Client()
        fingerprint = review_fingerprint(anonymous_client, token)

        response = anonymous_client.post(
            reverse("surface:client-approve", kwargs={"token": token}),
            {
                "batch_fingerprint": fingerprint,
                "item_ids": [
                    submitted_item.pk,
                    same_project_draft.pk,
                    other_project_item.pk,
                ],
            },
        )

        submitted_item.refresh_from_db()
        same_project_draft.refresh_from_db()
        other_project_item.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(submitted_item.state, AcceptanceItem.State.APPROVED)
        self.assertEqual(same_project_draft.state, AcceptanceItem.State.DRAFT)
        self.assertEqual(other_project_item.state, AcceptanceItem.State.DRAFT)

    @override_settings(**LOCMem_EMAIL)
    def test_stale_fingerprint_rejects_pull_back_and_resubmit_batch(self):
        """A newly minted submitted_at invalidates the previously rendered consent."""
        item = self.add_draft_item(text="Consent-bound criterion")
        fixed_submission_time = timezone.now()
        with patch(
            "ledger.services.timezone.now",
            return_value=fixed_submission_time,
        ):
            self.client.post(self.action_url("submit", item))
            token = make_client_token(self.project, "review")
            anonymous_client = Client()
            stale_fingerprint = review_fingerprint(anonymous_client, token)
            self.client.post(self.action_url("pull-back", item))
            self.client.post(self.action_url("submit", item))
        item.refresh_from_db()
        # Avoid the known Windows timezone.now() resolution flake recorded in PLAN.
        item.submitted_at += timedelta(seconds=1)
        item.save(update_fields=("submitted_at",))
        current_fingerprint = review_fingerprint(anonymous_client, token)
        self.assertNotEqual(stale_fingerprint, current_fingerprint)

        response = anonymous_client.post(
            reverse("surface:client-approve", kwargs={"token": token}),
            {"batch_fingerprint": stale_fingerprint},
        )

        item.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "criteria changed after this page was shown")
        self.assertContains(response, "Please re-review")
        self.assertEqual(item.state, AcceptanceItem.State.SUBMITTED)
        self.assertIsNone(item.approved_at)

        invalid_form_response = anonymous_client.post(
            reverse("surface:client-approve", kwargs={"token": token}),
            {},
        )
        self.assertEqual(invalid_form_response.status_code, 200)
        self.assertContains(
            invalid_form_response,
            "criteria changed after this page was shown",
        )
        self.assertNotContains(
            invalid_form_response,
            "This review link is no longer available",
        )

    @override_settings(**LOCMem_EMAIL)
    def test_one_stale_item_rejects_entire_submitted_batch(self):
        """Partial staleness leaves both the changed item and its peers unapproved."""
        items = [
            self.add_draft_item(text=f"Batch criterion {order}", order=order)
            for order in range(2, 5)
        ]
        for item in items:
            self.client.post(self.action_url("submit", item))
        token = make_client_token(self.project, "review")
        anonymous_client = Client()
        stale_fingerprint = review_fingerprint(anonymous_client, token)

        changed_item = items[1]
        self.client.post(self.action_url("pull-back", changed_item))
        self.client.post(self.action_url("submit", changed_item))
        changed_item.refresh_from_db()
        changed_item.submitted_at += timedelta(seconds=1)
        changed_item.save(update_fields=("submitted_at",))

        response = anonymous_client.post(
            reverse("surface:client-approve", kwargs={"token": token}),
            {"batch_fingerprint": stale_fingerprint},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "criteria changed after this page was shown")
        for item in items:
            item.refresh_from_db()
            self.assertEqual(item.state, AcceptanceItem.State.SUBMITTED)
            self.assertIsNone(item.approved_at)

    @override_settings(**LOCMem_EMAIL)
    def test_suspend_submitted_item_through_ui(self):
        """Submitted scope can be parked through its rendered action endpoint."""
        item = self.add_draft_item(text="Submitted then parked")
        self.client.post(self.action_url("submit", item))

        response = self.client.post(
            self.action_url("suspend", item),
            HTTP_HX_REQUEST="true",
        )

        item.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(item.state, AcceptanceItem.State.SUSPENDED)
        self.assertIsNone(item.approved_at)
        self.assertContains(response, "Resume")

    def test_illegal_item_transition_returns_404(self):
        """A new action endpoint deliberately conceals an illegal state edge."""
        response = self.client.post(
            self.action_url("submit", self.approved_item),
        )

        self.approved_item.refresh_from_db()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.approved_item.state, AcceptanceItem.State.APPROVED)


class ClientTokenPurposeIsolationTests(TestCase):
    """HTTP-level isolation between review and sign client tokens."""

    def setUp(self):
        """Active project with distinct review and sign tokens."""
        self.project = make_draft_project()
        submit_criteria_for_approval(self.project)
        approve_criteria(self.project)
        self.project.refresh_from_db()
        self.review_token = make_client_token(self.project, "review")
        self.sign_token = make_client_token(self.project, "sign")
        self.client = Client()

    def test_sign_token_cannot_open_client_review_page(self):
        """A sign-purpose token yields 410 on the review page."""
        response = self.client.get(
            reverse("surface:client-review", kwargs={"token": self.sign_token})
        )
        self.assertEqual(response.status_code, 410)
        self.assertContains(
            response,
            "This review link is no longer available",
            status_code=410,
        )

    def test_sign_token_cannot_post_client_approve(self):
        """A sign-purpose token yields 410 on criteria approval."""
        response = self.client.post(
            reverse("surface:client-approve", kwargs={"token": self.sign_token})
        )
        self.assertEqual(response.status_code, 410)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.ACTIVE)

    def test_sign_token_cannot_post_client_request_changes(self):
        """A sign-purpose token yields 410 on the request-changes action."""
        response = self.client.post(
            reverse(
                "surface:client-request-changes",
                kwargs={"token": self.sign_token},
            )
        )
        self.assertEqual(response.status_code, 410)

    def test_review_token_cannot_open_client_sign_page(self):
        """A review-purpose token yields 410 on the signing page."""
        advance_to_delivered(self.project)
        response = self.client.get(
            reverse("surface:client-sign", kwargs={"token": self.review_token})
        )
        self.assertEqual(response.status_code, 410)
        self.assertContains(
            response,
            "This review link is no longer available",
            status_code=410,
        )

    def test_review_token_cannot_post_client_sign(self):
        """A review-purpose token yields 410 when posting a signature."""
        advance_to_delivered(self.project)
        response = self.client.post(
            reverse("surface:client-sign", kwargs={"token": self.review_token}),
            {
                "signature_name": "Client Signer",
                "confirm": "on",
            },
        )
        self.assertEqual(response.status_code, 410)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.DELIVERED)


class DeliveryItemUpdateViewTests(TestCase):
    """Freelancer delivery checklist updates on active projects."""

    def setUp(self):
        """One active project with a single acceptance item."""
        self.owner = make_profile()
        self.project = make_draft_project(owner=self.owner)
        advance_to_active(self.project)
        self.item = self.project.acceptance_items.get()
        self.client = Client()

    def test_owner_can_update_delivery_item_when_active(self):
        """Owners may record pass/fail and evidence while the project is active."""
        login_as(self.client, self.owner)
        response = self.client.post(
            reverse(
                "surface:delivery-item-update",
                kwargs={"project_pk": self.project.pk, "item_pk": self.item.pk},
            ),
            delivery_form_data(
                is_passed="False",
                evidence_url="https://example.com/not-passed",
            ),
        )
        self.item.refresh_from_db()
        self.assertFalse(self.item.is_passed)
        self.assertEqual(self.item.evidence_url, "https://example.com/not-passed")
        self.assertRedirects(
            response,
            reverse("surface:project-detail", kwargs={"project_pk": self.project.pk}),
        )

    def test_delivery_update_returns_404_when_not_active(self):
        """Delivery updates are unavailable outside the active state."""
        mark_delivered(self.project)
        self.project.refresh_from_db()
        login_as(self.client, self.owner)
        response = self.client.post(
            reverse(
                "surface:delivery-item-update",
                kwargs={"project_pk": self.project.pk, "item_pk": self.item.pk},
            ),
            delivery_form_data(),
        )
        self.assertEqual(response.status_code, 404)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.is_passed)

    def test_non_owner_delivery_update_returns_404(self):
        """Non-owners cannot mutate another freelancer's delivery checklist."""
        other = make_profile(handle="delivery-other")
        login_as(self.client, other)
        response = self.client.post(
            reverse(
                "surface:delivery-item-update",
                kwargs={"project_pk": self.project.pk, "item_pk": self.item.pk},
            ),
            delivery_form_data(),
        )
        self.assertEqual(response.status_code, 404)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.is_passed)

    def test_invalid_evidence_url_rerenders_error_inside_criteria_panel(self):
        """A malformed evidence URL re-renders with the error inside the merged panel."""
        login_as(self.client, self.owner)
        response = self.client.post(
            reverse(
                "surface:delivery-item-update",
                kwargs={"project_pk": self.project.pk, "item_pk": self.item.pk},
            ),
            delivery_form_data(evidence_url="not-a-url"),
        )
        self.assertEqual(response.status_code, 200)
        panel = criteria_panel_html(response)
        self.assertIn("Enter a valid URL.", panel)
        self.assertContains(response, "Project brief")
        self.item.refresh_from_db()
        self.assertIsNone(self.item.is_passed)

    def test_delivery_update_refuses_suspended_item(self):
        """A parked item cannot be posted to the delivery ModelForm endpoint."""
        suspend_acceptance_item(self.item)
        login_as(self.client, self.owner)

        response = self.client.post(
            reverse(
                "surface:delivery-item-update",
                kwargs={"project_pk": self.project.pk, "item_pk": self.item.pk},
            ),
            delivery_form_data(),
        )

        self.item.refresh_from_db()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.item.state, AcceptanceItem.State.SUSPENDED)
        self.assertIsNone(self.item.is_passed)


class ProjectDetailPanelTests(TestCase):
    """Two-column detail layout and the merged criteria/delivery panel."""

    def setUp(self):
        """One owned project with a single criterion, viewed by its owner."""
        self.owner = make_profile()
        self.project = make_draft_project(owner=self.owner)
        self.item = self.project.acceptance_items.get()
        self.client = Client()
        login_as(self.client, self.owner)

    def get_detail(self):
        """GET the project detail page as the owner."""
        return self.client.get(
            reverse("surface:project-detail", kwargs={"project_pk": self.project.pk})
        )

    def test_detail_uses_two_column_layout(self):
        """The detail page renders the panelled grid inside the wide container."""
        response = self.get_detail()
        self.assertContains(response, 'class="container wide"')
        self.assertContains(response, 'class="project-layout"')

    def test_draft_detail_shows_criteria_editing_inside_layout_grid(self):
        """Draft projects keep criteria CRUD in the panelled grid, with no delivery form."""
        response = self.get_detail()
        content = response.content.decode()
        self.assertIn('class="project-layout"', content)
        self.assertLess(
            content.index('class="project-layout"'),
            content.index('id="criteria-panel"'),
        )
        panel = criteria_panel_html(response)
        self.assertIn("Add criterion", panel)
        self.assertNotIn("Save item", panel)
        for unavailable_control in {
            "Send for approval",
            "Pull back",
            "Suspend",
            "Withdraw",
            "Resume",
        }:
            self.assertNotIn(f">{unavailable_control}</button>", panel)
        self.assertNotContains(response, "Delivery checklist")

    def test_active_detail_merges_delivery_form_into_criteria_panel(self):
        """Active projects show the pass/evidence form inside the criteria panel."""
        advance_to_active(self.project)
        response = self.get_detail()
        update_url = reverse(
            "surface:delivery-item-update",
            kwargs={"project_pk": self.project.pk, "item_pk": self.item.pk},
        )
        panel = criteria_panel_html(response)
        self.assertIn(update_url, panel)
        self.assertIn("Save item", panel)
        self.assertIn("First criterion", panel)
        self.assertNotContains(response, "Delivery checklist")
        self.assertContains(response, "Add criterion")

    def test_delivered_detail_shows_read_only_results_with_evidence(self):
        """After delivery the panel lists results and evidence without forms."""
        advance_to_active(self.project)
        self.project.acceptance_items.update(
            is_passed=True,
            evidence_url="https://example.com/proof",
        )
        mark_delivered(self.project)
        response = self.get_detail()
        panel = criteria_panel_html(response)
        self.assertIn("Passed", panel)
        self.assertIn("https://example.com/proof", panel)
        for unavailable_control in {
            "Edit",
            "Delete",
            "Send for approval",
            "Pull back",
            "Suspend",
            "Save item",
            "Withdraw",
            "Resume",
        }:
            self.assertNotIn(f">{unavailable_control}</button>", panel)
        self.assertContains(response, "Awaiting signature")

    def test_active_panel_renders_controls_for_every_item_state(self):
        """Each item state exposes only its legal edit, review, and parking controls."""
        advance_to_active(self.project)
        timestamp = timezone.now()
        draft_item = AcceptanceItem.objects.create(
            project=self.project,
            text="Draft criterion",
            order=2,
        )
        submitted_item = AcceptanceItem.objects.create(
            project=self.project,
            text="Submitted criterion",
            order=3,
            state=AcceptanceItem.State.SUBMITTED,
            submitted_at=timestamp,
        )
        suspended_item = AcceptanceItem.objects.create(
            project=self.project,
            text="Suspended criterion",
            order=4,
            state=AcceptanceItem.State.SUSPENDED,
            submitted_at=timestamp,
            approved_at=timestamp,
        )
        withdrawn_item = AcceptanceItem.objects.create(
            project=self.project,
            text="Withdrawn criterion",
            order=5,
            state=AcceptanceItem.State.WITHDRAWN,
            submitted_at=timestamp,
            approved_at=timestamp,
        )

        response = self.get_detail()
        item_rows = {
            "Draft": (draft_item, {"Edit", "Delete", "Send for approval"}),
            "Submitted": (submitted_item, {"Pull back", "Suspend"}),
            "Approved": (self.item, {"Save item", "Suspend", "Withdraw"}),
            "Suspended": (suspended_item, {"Resume"}),
            "Withdrawn": (withdrawn_item, set()),
        }
        all_controls = {
            "Edit",
            "Delete",
            "Send for approval",
            "Pull back",
            "Suspend",
            "Save item",
            "Withdraw",
            "Resume",
        }
        for state_label, (item, legal_controls) in item_rows.items():
            item_html = criterion_html(response, item)
            self.assertIn(state_label, item_html)
            for control in all_controls:
                control_markup = f">{control}</button>"
                if control in legal_controls:
                    self.assertIn(control_markup, item_html)
                else:
                    self.assertNotIn(control_markup, item_html)


class MarkDeliveredViewTests(TestCase):
    """Marking an active project delivered and emailing the signing link."""

    @override_settings(**LOCMem_EMAIL)
    def test_mark_delivered_requires_all_items_decided(self):
        """Incomplete checklists stay active and do not send signing email."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        advance_to_active(project)
        client = Client()
        login_as(client, owner)
        client.get(reverse("surface:project-detail", kwargs={"project_pk": project.pk}))

        response = client.post(
            reverse("surface:mark-delivered", kwargs={"project_pk": project.pk})
        )

        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.ACTIVE)
        self.assertEqual(len(mail.outbox), 0)
        follow = client.get(response.url)
        self.assertContains(
            follow,
            "Review every delivery item before marking the project delivered.",
        )

    @override_settings(**LOCMem_EMAIL)
    def test_mark_delivered_transitions_and_emails_sign_link(self):
        """A fully reviewed checklist moves to delivered and emails /client/sign/."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        advance_to_active(project)
        item = project.acceptance_items.get()
        client = Client()
        login_as(client, owner)
        client.get(reverse("surface:project-detail", kwargs={"project_pk": project.pk}))
        client.post(
            reverse(
                "surface:delivery-item-update",
                kwargs={"project_pk": project.pk, "item_pk": item.pk},
            ),
            delivery_form_data(),
        )

        response = client.post(
            reverse("surface:mark-delivered", kwargs={"project_pk": project.pk}),
            follow=True,
        )

        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.DELIVERED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/client/sign/", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, [project.client_email])
        self.assertRedirects(
            response,
            reverse("surface:project-detail", kwargs={"project_pk": project.pk}),
        )
        self.assertContains(response, "Delivery recorded and sent for signature.")

    @override_settings(**LOCMem_EMAIL)
    def test_signing_email_keeps_action_first_and_discovers_portal(self):
        """The actual delivery view includes both signing and portal URLs."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        advance_to_active(project)
        project.acceptance_items.update(is_passed=True)
        client = Client()
        login_as(client, owner)

        client.post(
            reverse("surface:mark-delivered", kwargs={"project_pk": project.pk})
        )

        body_lines = [
            line for line in mail.outbox[0].body.splitlines() if line.strip()
        ]
        self.assertIn("/client/sign/", body_lines[0])
        self.assertTrue(
            any("/portal/login/" in line for line in body_lines[1:]),
            "Signing email did not include portal discovery after its action URL.",
        )

    @override_settings(**LOCMem_EMAIL)
    def test_mark_delivered_rejects_failed_item_with_clear_message(self):
        """A failed checklist item blocks delivery and explains the policy."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        advance_to_active(project)
        project.acceptance_items.update(is_passed=False)
        client = Client()
        login_as(client, owner)

        response = client.post(
            reverse("surface:mark-delivered", kwargs={"project_pk": project.pk}),
            follow=True,
        )

        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.ACTIVE)
        self.assertEqual(len(mail.outbox), 0)
        self.assertContains(
            response,
            "Every delivery item must pass before delivery.",
        )

    @override_settings(**LOCMem_EMAIL)
    def test_delivery_gating_ignores_suspended_and_withdrawn_items(self):
        """Parked items neither disable the button nor block the delivery POST."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        advance_to_active(project)
        approved_item = project.acceptance_items.get()
        approved_item.is_passed = True
        approved_item.save(update_fields=("is_passed",))
        timestamp = timezone.now()
        suspended_item = AcceptanceItem.objects.create(
            project=project,
            text="Parked temporarily",
            order=2,
            state=AcceptanceItem.State.APPROVED,
            submitted_at=timestamp,
            approved_at=timestamp,
        )
        withdrawn_item = AcceptanceItem.objects.create(
            project=project,
            text="Removed from scope",
            order=3,
            state=AcceptanceItem.State.APPROVED,
            submitted_at=timestamp,
            approved_at=timestamp,
        )
        suspend_acceptance_item(suspended_item)
        withdraw_acceptance_item(withdrawn_item)
        client = Client()
        login_as(client, owner)

        detail_response = client.get(
            reverse("surface:project-detail", kwargs={"project_pk": project.pk})
        )
        self.assertNotContains(
            detail_response,
            '<button type="submit" disabled>Mark delivered and send</button>',
            html=True,
        )
        response = client.post(
            reverse("surface:mark-delivered", kwargs={"project_pk": project.pk})
        )

        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.DELIVERED)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(**LOCMem_EMAIL)
    def test_non_owner_cannot_mark_delivered(self):
        """Another freelancer cannot mark a project delivered or trigger email."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        advance_to_active(project)
        mark_all_items_passed(project)
        other = make_profile()
        client = Client()
        login_as(client, other)
        response = client.post(
            reverse("surface:mark-delivered", kwargs={"project_pk": project.pk})
        )
        project.refresh_from_db()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(project.status, Project.Status.ACTIVE)
        self.assertEqual(len(mail.outbox), 0)


class ClientSignViewTests(TestCase):
    """Client signing of delivered projects via purpose-bound tokens."""

    def setUp(self):
        """Delivered project with a fresh sign token."""
        self.project = make_draft_project()
        advance_to_delivered(self.project)
        self.sign_token = make_client_token(self.project, "sign")
        self.client = Client()

    def get_sign_page_with_parked_item(self, state, is_passed):
        """Render a legal delivered project containing one parked criterion."""
        project = make_draft_project()
        parked_item = AcceptanceItem.objects.create(
            project=project,
            text="Adjusted scope criterion",
            order=2,
        )
        advance_to_active(project)
        parked_item.refresh_from_db()
        parked_item.is_passed = is_passed
        parked_item.save(update_fields=("is_passed",))
        if state == AcceptanceItem.State.SUSPENDED:
            suspend_acceptance_item(parked_item)
        else:
            withdraw_acceptance_item(parked_item)
        project.acceptance_items.filter(
            state=AcceptanceItem.State.APPROVED
        ).update(is_passed=True)
        mark_delivered(project)
        token = make_client_token(project, "sign")
        return self.client.get(
            reverse("surface:client-sign", kwargs={"token": token})
        )

    def test_suspended_failed_item_is_disclosed_without_not_passed_label(self):
        """A suspended failure is outside delivery, with its result secondary."""
        response = self.get_sign_page_with_parked_item(
            AcceptanceItem.State.SUSPENDED,
            False,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="badge parked"')
        self.assertContains(response, "Suspended — excluded from this delivery")
        self.assertContains(response, "Recorded result:")
        self.assertContains(response, "Failed")
        self.assertNotContains(response, "Not passed")

    def test_withdrawn_passed_item_is_disclosed_without_not_passed_label(self):
        """Withdrawn scope is named distinctly and retains its recorded result."""
        response = self.get_sign_page_with_parked_item(
            AcceptanceItem.State.WITHDRAWN,
            True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="badge parked"')
        self.assertContains(response, "Withdrawn — excluded from this delivery")
        self.assertContains(response, "Recorded result:")
        self.assertContains(response, "Passed")
        self.assertNotContains(response, "Not passed")

    def test_parked_item_steps_render_inside_excluded_scope_framing(self):
        """Suspended scope retains its steps within the parked visual group."""
        project = make_draft_project()
        parked_item = AcceptanceItem.objects.create(
            project=project,
            text="Parked criterion with steps",
            order=2,
        )
        AcceptanceStep.objects.create(
            item=parked_item,
            text="Parked nested step",
            order=1,
        )
        advance_to_active(project)
        parked_item.refresh_from_db()
        suspend_acceptance_item(parked_item)
        project.acceptance_items.filter(
            state=AcceptanceItem.State.APPROVED
        ).update(is_passed=True)
        mark_delivered(project)
        token = make_client_token(project, "sign")

        response = self.client.get(
            reverse("surface:client-sign", kwargs={"token": token})
        )
        content = response.content.decode()
        parked_start = content.index('class="parked-scope"')
        parked_end = content.index("</div>", parked_start)

        self.assertContains(response, "Parked nested step")
        self.assertGreater(content.index("Parked nested step"), parked_start)
        self.assertLess(content.index("Parked nested step"), parked_end)

    def test_unrecorded_result_is_never_presented_as_not_passed(self):
        """An undecided parked criterion says no result instead of inventing failure."""
        response = self.get_sign_page_with_parked_item(
            AcceptanceItem.State.SUSPENDED,
            None,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Suspended — excluded from this delivery")
        self.assertContains(response, "Recorded result:")
        self.assertContains(response, "No result recorded")
        self.assertNotContains(response, "Not passed")

    def test_ordinary_unrecorded_result_is_not_presented_as_not_passed(self):
        """An undecided approved item is not mislabeled as a delivery failure."""
        self.project.acceptance_items.update(is_passed=None)

        response = self.client.get(
            reverse("surface:client-sign", kwargs={"token": self.sign_token})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<strong>No result recorded</strong>", html=True)
        self.assertNotContains(response, "Not passed")

    def test_passed_item_keeps_passed_label_and_evidence_link(self):
        """Ordinary completed scope remains passed with its evidence available."""
        evidence_url = "https://example.com/client-proof"
        self.project.acceptance_items.update(evidence_url=evidence_url)

        response = self.client.get(
            reverse("surface:client-sign", kwargs={"token": self.sign_token})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<strong>Passed</strong>", html=True)
        self.assertContains(response, evidence_url)
        self.assertContains(response, "View evidence")

    def test_passed_item_with_outstanding_step_discloses_both_facts_prominently(self):
        """The outstanding-work alert precedes Passed and names the undone step."""
        item = self.project.acceptance_items.get()
        AcceptanceStep.objects.create(
            item=item,
            text="Outstanding signing step",
            order=1,
            is_done=False,
        )

        response = self.client.get(
            reverse("surface:client-sign", kwargs={"token": self.sign_token})
        )
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'class="outstanding-steps-notice" role="alert"',
        )
        self.assertContains(response, "this criterion is marked Passed")
        self.assertContains(response, "Outstanding signing step")
        self.assertContains(response, "Not done")
        self.assertContains(response, "<strong>Passed</strong>", html=True)
        self.assertLess(
            content.index('class="outstanding-steps-notice"'),
            content.index("<strong>Passed</strong>"),
        )

    def test_outstanding_step_notice_has_primary_visual_treatment(self):
        """The consent warning is styled as a strong alert, not muted metadata."""
        stylesheet = (
            settings.BASE_DIR / "static" / "css" / "app.css"
        ).read_text(encoding="utf-8")
        notice_rule = stylesheet.split(
            ".outstanding-steps-notice {",
            1,
        )[1].split("}", 1)[0]

        self.assertIn("border: 2px solid", notice_rule)
        self.assertIn("background: #fff1d6", notice_rule)
        self.assertIn("font-weight: 650", notice_rule)

    def test_mobile_criterion_grid_has_no_dead_flex_direction(self):
        """Responsive criterion CSS does not retain a flex-only declaration."""
        stylesheet = (
            settings.BASE_DIR / "static" / "css" / "app.css"
        ).read_text(encoding="utf-8")
        mobile_rules = stylesheet.split("@media (max-width: 680px) {", 1)[1]
        criterion_rule = mobile_rules.split(".criterion {", 1)[1].split(
            "}",
            1,
        )[0]

        self.assertNotIn("flex-direction", criterion_rule)

    def test_passed_item_with_all_steps_done_omits_outstanding_notice(self):
        """Completed granular work does not trigger the outstanding-work warning."""
        item = self.project.acceptance_items.get()
        AcceptanceStep.objects.create(
            item=item,
            text="Completed signing step",
            order=1,
            is_done=True,
        )

        response = self.client.get(
            reverse("surface:client-sign", kwargs={"token": self.sign_token})
        )

        self.assertContains(response, "Completed signing step")
        self.assertContains(response, "Done")
        self.assertNotContains(response, 'class="outstanding-steps-notice"')

    def test_signing_page_shows_done_and_not_done_step_states(self):
        """Every signing-page step carries its explicit completion state."""
        item = self.project.acceptance_items.get()
        AcceptanceStep.objects.create(
            item=item,
            text="Completed signing step",
            order=1,
            is_done=True,
        )
        AcceptanceStep.objects.create(
            item=item,
            text="Incomplete signing step",
            order=2,
            is_done=False,
        )

        response = self.client.get(
            reverse("surface:client-sign", kwargs={"token": self.sign_token})
        )

        self.assertContains(response, "Completed signing step")
        self.assertContains(response, "Incomplete signing step")
        self.assertContains(response, "Done")
        self.assertContains(response, "Not done")

    def test_signing_page_omits_step_block_when_criterion_has_no_steps(self):
        """Empty step scope adds no noisy placeholder to the consent record."""
        response = self.client.get(
            reverse("surface:client-sign", kwargs={"token": self.sign_token})
        )

        self.assertNotContains(response, 'class="criterion-steps"')
        self.assertNotContains(response, 'class="outstanding-steps-notice"')

    def test_client_sign_happy_path_attests_and_shows_payload_hash(self):
        """Valid signature posts through ledger and renders the record hash."""
        response = self.client.post(
            reverse("surface:client-sign", kwargs={"token": self.sign_token}),
            {
                "signature_name": "Acme Authorized Signer",
                "confirm": "on",
            },
        )
        self.project.refresh_from_db()
        attestation = self.project.attestations.get(is_current=True)
        self.assertEqual(self.project.status, Project.Status.ATTESTED)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Signature recorded")
        self.assertContains(response, "Thank you")
        self.assertContains(response, attestation.payload_hash)

    def test_client_sign_records_project_client_email_exactly(self):
        """Token signing records the project's exact client email spelling."""
        self.project.client_email = "Client.MixedCase@Example.COM"
        self.project.save(update_fields=("client_email",))

        response = self.client.post(
            reverse("surface:client-sign", kwargs={"token": self.sign_token}),
            {
                "signature_name": "Acme Authorized Signer",
                "confirm": "on",
            },
        )

        attestation = self.project.attestations.get(is_current=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(attestation.client_email, "Client.MixedCase@Example.COM")

    def test_client_sign_missing_confirm_is_rejected(self):
        """Signing without the confirmation checkbox does not create an attestation."""
        response = self.client.post(
            reverse("surface:client-sign", kwargs={"token": self.sign_token}),
            {"signature_name": "Acme Authorized Signer"},
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.DELIVERED)
        self.assertFalse(self.project.attestations.exists())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required")
        self.assertContains(response, "Type your full name")

    def test_client_sign_non_delivered_active_project_returns_410(self):
        """Signing is unavailable before the project is marked delivered."""
        reopen_project = make_draft_project()
        advance_to_active(reopen_project)
        token = make_client_token(reopen_project, "sign")
        response = self.client.get(
            reverse("surface:client-sign", kwargs={"token": token})
        )
        self.assertEqual(response.status_code, 410)

    def test_client_sign_post_non_delivered_never_calls_signing_service(self):
        """A valid POST cannot reach Ledger before delivery."""
        active_project = make_draft_project()
        advance_to_active(active_project)
        token = make_client_token(active_project, "sign")

        with patch("ledger.services.sign_attestation") as sign_record:
            response = self.client.post(
                reverse("surface:client-sign", kwargs={"token": token}),
                {
                    "signature_name": "Acme Authorized Signer",
                    "confirm": "on",
                },
            )

        sign_record.assert_not_called()
        self.assertEqual(response.status_code, 410)
        active_project.refresh_from_db()
        self.assertEqual(active_project.status, Project.Status.ACTIVE)
        self.assertFalse(active_project.attestations.exists())

    def test_client_sign_already_attested_shows_signed_page(self):
        """An already attested project shows the signed confirmation page."""
        attestation = sign_project_via_service(self.project)
        response = self.client.get(
            reverse("surface:client-sign", kwargs={"token": self.sign_token})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Thank you")
        self.assertContains(response, attestation.payload_hash)
        self.assertNotContains(response, "Type your full name")

    def test_client_sign_post_already_attested_reuses_signed_record(self):
        """A repeated valid POST shows the existing record without signing again."""
        attestation = sign_project_via_service(self.project)

        with patch("ledger.services.sign_attestation") as sign_record:
            response = self.client.post(
                reverse("surface:client-sign", kwargs={"token": self.sign_token}),
                {
                    "signature_name": "Different Signer",
                    "confirm": "on",
                },
            )

        sign_record.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, attestation.payload_hash)
        self.assertEqual(self.project.attestations.count(), 1)

    def test_client_sign_invalid_transition_returns_token_error(self):
        """A delivery race returns the generic unavailable response without a record."""
        with patch(
            "ledger.services.sign_attestation",
            side_effect=InvalidTransition("status changed"),
        ):
            response = self.client.post(
                reverse("surface:client-sign", kwargs={"token": self.sign_token}),
                {
                    "signature_name": "Acme Authorized Signer",
                    "confirm": "on",
                },
            )

        self.assertEqual(response.status_code, 410)
        self.assertContains(
            response,
            "This review link is no longer available",
            status_code=410,
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.DELIVERED)
        self.assertFalse(self.project.attestations.exists())

    def test_client_sign_empty_signature_is_rejected(self):
        """Blank signature names re-render the form with validation errors."""
        response = self.client.post(
            reverse("surface:client-sign", kwargs={"token": self.sign_token}),
            {
                "signature_name": "",
                "confirm": "on",
            },
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.DELIVERED)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required")
        self.assertContains(response, "Type your full name")


class PublicRecordViewTests(TestCase):
    """Public capability record rendering without client secrets."""

    def setUp(self):
        """Profile with one clean attestation and one disputed attestation."""
        self.owner = make_profile(handle="public-freelancer")
        set_profile_visibility(self.owner, True)
        self.clean_project = make_draft_project(
            owner=self.owner,
            with_criteria=True,
        )
        self.clean_project.title = "Clean Public Project"
        self.clean_project.save(update_fields=("title",))
        self.clean_attestation = sign_project_via_service(
            self.clean_project,
            client_name="Clean Client Name",
        )

        self.disputed_project = make_draft_project(
            owner=self.owner,
            with_criteria=True,
        )
        self.disputed_project.title = "Disputed Public Project"
        self.disputed_project.save(update_fields=("title",))
        sign_project_via_service(
            self.disputed_project,
            client_name="Disputed Client Name",
        )
        flag_dispute(self.disputed_project)

        self.review_token = make_client_token(self.clean_project, "review")
        self.sign_token = make_client_token(self.clean_project, "sign")
        self.client = Client()

    def create_payload_attestation(self, title, acceptance_items):
        """Create a current attestation from an explicit signed payload fixture."""
        project = Project.objects.create(
            owner=self.owner,
            title=title,
            client_name="Payload Fixture Client",
            client_email="payload-fixture@example.com",
            brief="Payload fixture brief",
            skills_csv="",
            status=Project.Status.ATTESTED,
        )
        payload = {
            "acceptance_items": acceptance_items,
            "approved_change_orders": [],
            "brief": project.brief,
            "revision_limit": project.revision_limit,
            "skills": [],
            "title": title,
        }
        return project.attestations.create(
            payload=payload,
            payload_hash=compute_payload_hash(payload),
            client_email=project.client_email,
            client_name_typed="Payload Fixture Signer",
            signature_meta={},
        )

    def get_public_record(self):
        """Render the published record for this test profile."""
        return self.client.get(
            reverse("surface:public-record", kwargs={"handle": self.owner.handle})
        )

    def attestation_markup(self, response, title):
        """Return the rendered article content following one attestation title."""
        return response.content.decode().split(title, 1)[1].split("</article>", 1)[0]

    def test_legacy_payload_without_state_renders_without_parked_notice(self):
        """Missing legacy item state is treated as active, not parked."""
        title = "Legacy Payload Project"
        legacy_attestation = self.create_payload_attestation(
            title,
            [
                {
                    "text": "Legacy criterion",
                    "is_passed": True,
                    "evidence_url": "",
                }
            ],
        )
        for item in legacy_attestation.payload["acceptance_items"]:
            self.assertNotIn("state", item)

        response = self.get_public_record()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, title)
        self.assertNotIn("Scope adjusted", self.attestation_markup(response, title))

    def test_suspended_item_discloses_parked_criterion_count(self):
        """A suspended payload item produces the generic scope-adjustment notice."""
        title = "Suspended Payload Project"
        self.create_payload_attestation(
            title,
            [
                {
                    "text": "Parked criterion",
                    "state": AcceptanceItem.State.SUSPENDED,
                    "is_passed": False,
                    "evidence_url": "",
                }
            ],
        )

        markup = self.attestation_markup(self.get_public_record(), title)

        self.assertIn("Scope adjusted", markup)
        self.assertIn(
            "1 acceptance criterion\n              was parked or withdrawn",
            markup,
        )

    def test_withdrawn_item_discloses_parked_criterion_count(self):
        """A withdrawn payload item produces the generic scope-adjustment notice."""
        title = "Withdrawn Payload Project"
        self.create_payload_attestation(
            title,
            [
                {
                    "text": "Withdrawn criterion",
                    "state": AcceptanceItem.State.WITHDRAWN,
                    "is_passed": False,
                    "evidence_url": "",
                }
            ],
        )

        markup = self.attestation_markup(self.get_public_record(), title)

        self.assertIn("Scope adjusted", markup)
        self.assertIn("1 acceptance criterion", markup)
        self.assertIn("was parked or withdrawn", markup)

    def test_all_approved_items_show_no_parked_notice(self):
        """Approved payload items do not imply any signed scope adjustment."""
        title = "Approved Payload Project"
        self.create_payload_attestation(
            title,
            [
                {
                    "text": "Approved criterion",
                    "state": AcceptanceItem.State.APPROVED,
                    "is_passed": True,
                    "evidence_url": "",
                }
            ],
        )

        response = self.get_public_record()

        self.assertContains(response, title)
        self.assertNotIn("Scope adjusted", self.attestation_markup(response, title))

    def test_legacy_and_new_payloads_disclose_only_parked_attestation(self):
        """Mixed payload generations are counted independently per attestation."""
        legacy_title = "Mixed Legacy Payload"
        parked_title = "Mixed Parked Payload"
        self.create_payload_attestation(
            legacy_title,
            [{"text": "Legacy", "is_passed": True, "evidence_url": ""}],
        )
        self.create_payload_attestation(
            parked_title,
            [
                {
                    "text": "Suspended",
                    "state": AcceptanceItem.State.SUSPENDED,
                    "is_passed": False,
                    "evidence_url": "",
                },
                {
                    "text": "Withdrawn",
                    "state": AcceptanceItem.State.WITHDRAWN,
                    "is_passed": True,
                    "evidence_url": "",
                },
            ],
        )

        response = self.get_public_record()

        self.assertNotIn(
            "Scope adjusted",
            self.attestation_markup(response, legacy_title),
        )
        parked_markup = self.attestation_markup(response, parked_title)
        self.assertIn("Scope adjusted", parked_markup)
        self.assertIn("2 acceptance criteria", parked_markup)
        self.assertIn("were parked or withdrawn", parked_markup)

    def test_failed_criterion_suspended_before_signing_is_disclosed_on_public_record(self):
        """A signed project cannot hide a failed criterion that was suspended."""
        project = make_draft_project(owner=self.owner)
        project.title = "Suspended Failure Pipeline Project"
        project.save(update_fields=("title",))
        AcceptanceItem.objects.create(
            project=project,
            text="Second criterion",
            order=2,
        )
        submit_criteria_for_approval(project)
        approve_criteria(project)
        passed_item, failed_item = project.acceptance_items.order_by("order")
        passed_item.is_passed = True
        passed_item.save(update_fields=("is_passed",))
        failed_item.is_passed = False
        failed_item.save(update_fields=("is_passed",))
        suspend_acceptance_item(failed_item)
        mark_delivered(project)

        attestation = sign_attestation(
            project,
            project.client_email,
            "Pipeline Test Signer",
            {},
        )

        self.assertEqual(
            [
                item.get("state")
                for item in attestation.payload["acceptance_items"]
            ],
            [
                AcceptanceItem.State.APPROVED,
                AcceptanceItem.State.SUSPENDED,
            ],
        )
        markup = self.attestation_markup(self.get_public_record(), project.title)
        self.assertIn("Scope adjusted", markup)

    def test_delivered_project_cannot_delete_parked_scope_before_signing(self):
        """Delivered scope remains frozen into the signed public disclosure."""
        project = make_draft_project(owner=self.owner)
        project.title = "Frozen Scope Pipeline Project"
        project.save(update_fields=("title",))
        parked_item = AcceptanceItem.objects.create(
            project=project,
            text="Parked before delivery",
            order=2,
        )
        submit_criteria_for_approval(project)
        parked_item.refresh_from_db()
        suspend_acceptance_item(parked_item)
        approve_criteria(project)
        project.acceptance_items.filter(
            state=AcceptanceItem.State.APPROVED,
        ).update(is_passed=True)
        mark_delivered(project)

        login_as(self.client, self.owner)
        delete_response = self.client.post(
            reverse(
                "surface:criterion-delete",
                kwargs={"project_pk": project.pk, "item_pk": parked_item.pk},
            )
        )
        sign_attestation(
            project,
            project.client_email,
            "Frozen Scope Signer",
            {},
        )

        markup = self.attestation_markup(self.get_public_record(), project.title)
        self.assertIn("Scope adjusted", markup)
        self.assertEqual(delete_response.status_code, 404)
        self.assertTrue(
            AcceptanceItem.objects.filter(pk=parked_item.pk).exists()
        )

    def test_anonymous_unpublished_record_returns_404(self):
        """Anonymous visitors cannot discover an unpublished record."""
        set_profile_visibility(self.owner, False)

        response = self.client.get(
            reverse("surface:public-record", kwargs={"handle": self.owner.handle})
        )

        self.assertEqual(response.status_code, 404)

    def test_anonymous_published_record_is_indexable(self):
        """Anonymous visitors receive a published record without noindex."""
        response = self.client.get(
            reverse("surface:public-record", kwargs={"handle": self.owner.handle})
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("X-Robots-Tag", response)

    def test_authenticated_non_owner_cannot_view_unpublished_record(self):
        """Another freelancer receives 404 for an unpublished record."""
        set_profile_visibility(self.owner, False)
        other = make_profile(handle="record-viewer")
        login_as(self.client, other)

        response = self.client.get(
            reverse("surface:public-record", kwargs={"handle": self.owner.handle})
        )

        self.assertEqual(response.status_code, 404)

    def test_owner_sees_unpublished_preview_with_noindex(self):
        """The owner sees an unmistakable private preview that cannot be indexed."""
        set_profile_visibility(self.owner, False)
        login_as(self.client, self.owner)

        response = self.client.get(
            reverse("surface:public-record", kwargs={"handle": self.owner.handle})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Not published")
        self.assertContains(response, "only you can see this private preview")
        self.assertContains(response, "Publish record")
        self.assertContains(response, 'name="intent"')
        self.assertContains(response, 'value="publish"')
        self.assertContains(
            response,
            reverse(
                "surface:profile-visibility",
                kwargs={"handle": self.owner.handle},
            ),
        )
        self.assertEqual(response["X-Robots-Tag"], "noindex")

    def test_anonymous_published_record_hides_owner_controls(self):
        """Published visitor pages contain no visibility form or owner-only copy."""
        response = self.client.get(
            reverse("surface:public-record", kwargs={"handle": self.owner.handle})
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Record visibility")
        self.assertNotContains(response, "Publish record")
        self.assertNotContains(response, "Unpublish record")
        self.assertNotContains(
            response,
            reverse(
                "surface:profile-visibility",
                kwargs={"handle": self.owner.handle},
            ),
        )

    def test_authenticated_non_owner_sees_published_record_without_owner_controls(self):
        """A logged-in visitor sees a published record without owner UI or noindex."""
        other = make_profile(handle="published-viewer")
        login_as(self.client, other)

        response = self.client.get(
            reverse("surface:public-record", kwargs={"handle": self.owner.handle})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.clean_project.title)
        self.assertNotContains(response, "Record visibility")
        self.assertNotContains(response, "Publish record")
        self.assertNotContains(response, "Unpublish record")
        self.assertNotIn("X-Robots-Tag", response)

    def test_authenticated_non_owner_sees_dispute_notice_on_published_record(self):
        """Dispute-freeze copy on a published record is unchanged for logged-in visitors."""
        other = make_profile(handle="dispute-viewer")
        login_as(self.client, other)

        response = self.client.get(
            reverse("surface:public-record", kwargs={"handle": self.owner.handle})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "dispute-notice")
        self.assertContains(response, "1 signed record currently")
        self.assertContains(response, "withheld from the verified record below")
        self.assertNotContains(response, self.disputed_project.title)

    def test_owner_unpublished_preview_renders_tags_and_attestations(self):
        """Private preview shows verified skills and clean attestations before publishing."""
        set_profile_visibility(self.owner, False)
        login_as(self.client, self.owner)

        response = self.client.get(
            reverse("surface:public-record", kwargs={"handle": self.owner.handle})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Not published")
        self.assertContains(response, self.clean_project.title)
        self.assertContains(response, self.clean_attestation.payload_hash)
        self.assertContains(response, "Verified skills")
        self.assertContains(response, "django · 1")

    def test_public_record_shows_clean_attestation_and_withholds_disputed(self):
        """Public page lists clean attestations, hides disputed ones, and omits PII."""
        response = self.client.get(
            reverse("surface:public-record", kwargs={"handle": self.owner.handle})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.clean_project.title)
        self.assertContains(response, self.clean_attestation.payload_hash)
        self.assertNotContains(response, self.disputed_project.title)
        self.assertNotContains(response, self.clean_project.client_email)
        self.assertNotContains(response, "Clean Client Name")
        self.assertNotContains(response, "Disputed Client Name")
        self.assertNotContains(response, self.review_token)
        self.assertNotContains(response, self.sign_token)
        self.assertContains(response, "dispute-notice")
        self.assertContains(response, "1 signed record currently")
        self.assertContains(response, "withheld from the verified record below")

    def test_public_record_displays_signed_skills_after_live_project_edit(self):
        """Public skill text comes from the signed payload, not mutable project data."""
        self.clean_project.skills_csv = "python"
        self.clean_project.save(update_fields=("skills_csv", "updated_at"))

        response = self.client.get(
            reverse("surface:public-record", kwargs={"handle": self.owner.handle})
        )

        self.assertContains(response, "<strong>Skills:</strong> django", html=True)
        self.assertNotContains(response, "<strong>Skills:</strong> python", html=True)


class ProfileVisibilityViewTests(TestCase):
    """Owner-only publication changes for the Capability Record."""

    def setUp(self):
        """Create one private profile and its visibility action URL."""
        self.owner = make_profile(handle="visibility-owner")
        self.url = reverse(
            "surface:profile-visibility",
            kwargs={"handle": self.owner.handle},
        )

    def test_owner_post_publishes_then_unpublishes_record(self):
        """Explicit owner actions set visibility both ways through the endpoint."""
        client = Client()
        login_as(client, self.owner)

        with patch(
            "surface.views.services.set_profile_visibility",
            wraps=set_profile_visibility,
        ) as visibility_service:
            publish_response = client.post(self.url, {"intent": "publish"})
            self.owner.refresh_from_db()
            self.assertTrue(self.owner.is_public)
            self.assertRedirects(
                publish_response,
                reverse("surface:public-record", kwargs={"handle": self.owner.handle}),
            )
            published_page = client.get(
                reverse("surface:public-record", kwargs={"handle": self.owner.handle})
            )
            self.assertContains(published_page, "Unpublish record")
            self.assertContains(published_page, 'value="unpublish"')

            unpublish_response = client.post(self.url, {"intent": "unpublish"})
            self.owner.refresh_from_db()
            self.assertFalse(self.owner.is_public)
            self.assertRedirects(
                unpublish_response,
                reverse("surface:public-record", kwargs={"handle": self.owner.handle}),
            )

        self.assertEqual(
            [service_call.args[1] for service_call in visibility_service.call_args_list],
            [True, False],
        )

    def test_repeated_visibility_intent_is_idempotent(self):
        """Repeating either explicit intent cannot reverse the requested state."""
        client = Client()
        login_as(client, self.owner)

        client.post(self.url, {"intent": "publish"})
        client.post(self.url, {"intent": "publish"})
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_public)

        client.post(self.url, {"intent": "unpublish"})
        client.post(self.url, {"intent": "unpublish"})
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_public)

    def test_invalid_or_missing_intent_does_not_change_visibility(self):
        """Malformed owner actions return 404 without changing visibility."""
        client = Client()
        login_as(client, self.owner)

        for post_data in ({"intent": "invalid"}, {}):
            with self.subTest(post_data=post_data):
                set_profile_visibility(self.owner, False)
                response = client.post(self.url, post_data)
                self.assertEqual(response.status_code, 404)
                self.owner.refresh_from_db()
                self.assertFalse(self.owner.is_public)

    def test_non_owner_and_anonymous_posts_cannot_change_visibility(self):
        """Valid intent from non-owners is rejected before visibility can change."""
        other = make_profile(handle="visibility-other")
        other_client = Client()
        login_as(other_client, other)

        for client, label in (
            (other_client, "authenticated non-owner"),
            (Client(), "anonymous"),
        ):
            with self.subTest(case=label):
                set_profile_visibility(self.owner, False)
                response = client.post(self.url, {"intent": "publish"})
                self.assertEqual(response.status_code, 404)
                self.owner.refresh_from_db()
                self.assertFalse(self.owner.is_public)

    def test_owner_post_requires_csrf_token(self):
        """CSRF middleware rejects an owner visibility action without a token."""
        client = Client(enforce_csrf_checks=True)
        login_as(client, self.owner)

        response = client.post(self.url, {"intent": "publish"})

        self.assertEqual(response.status_code, 403)
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_public)


class ProfileAutoCreationTests(TestCase):
    """Surface-created profiles retain Ledger's private default."""

    def test_get_or_create_freelancer_does_not_override_private_default(self):
        """The Surface auth call site creates a profile with is_public false."""
        user = get_or_create_freelancer("private-by-default@example.com")

        self.assertFalse(user.profile.is_public)


class RecordRedirectViewTests(TestCase):
    """Authenticated redirect from dashboard record entry point."""

    def test_logged_in_owner_redirects_to_public_record(self):
        """Owners are sent to their public /u/<handle>/ capability record."""
        owner = make_profile(handle="record-owner")
        client = Client()
        login_as(client, owner)
        response = client.get(reverse("surface:record"))
        self.assertRedirects(
            response,
            reverse("surface:public-record", kwargs={"handle": owner.handle}),
        )

    def test_anonymous_record_redirects_to_login(self):
        """Unauthenticated visitors cannot use the dashboard record shortcut."""
        response = Client().get(reverse("surface:record"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("surface:login-request"), response.url)

    def test_private_owner_record_redirect_lands_on_usable_preview(self):
        """Following /record/ as a private-profile owner yields a 200 preview, not 404."""
        owner = make_profile(handle="private-record-owner")
        client = Client()
        login_as(client, owner)

        response = client.get(reverse("surface:record"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Not published")
        self.assertContains(response, "only you can see this private preview")
        self.assertEqual(response["X-Robots-Tag"], "noindex")


class ProjectDetailPublicRecordLinkTests(TestCase):
    """Project detail link to the owner's Capability Record."""

    def test_project_detail_public_record_link_works_when_unpublished(self):
        """Owner following the attested-project link reaches their private preview."""
        owner = make_profile(handle="detail-link-owner")
        project = make_draft_project(owner=owner)
        sign_project_via_service(project)
        client = Client()
        login_as(client, owner)
        record_url = reverse("surface:public-record", kwargs={"handle": owner.handle})

        detail = client.get(
            reverse("surface:project-detail", kwargs={"project_pk": project.pk})
        )

        self.assertContains(detail, "View the public record")
        self.assertContains(detail, record_url)
        record_response = client.get(record_url)
        self.assertEqual(record_response.status_code, 200)
        self.assertContains(record_response, "Not published")
        self.assertContains(record_response, project.title)


class ProjectEntitlementTests(TestCase):
    """Project creation requires a Pro subscription or project-pack credit."""

    @override_settings(**BILLING_STUB_SETTINGS)
    def test_project_create_blocked_without_entitlement(self):
        """Creating a project without billing entitlement redirects to billing."""
        owner = make_profile()
        client = Client()
        login_as(client, owner, grant_entitlement=False)

        response = client.post(
            reverse("surface:project-create"),
            project_form_data(title="Blocked Without Plan"),
            follow=True,
        )

        self.assertRedirects(response, reverse("surface:billing"))
        self.assertFalse(
            Project.objects.filter(title="Blocked Without Plan").exists()
        )
        self.assertContains(response, "Choose a plan before creating a project.")

    @override_settings(**BILLING_STUB_SETTINGS)
    def test_project_create_allowed_after_stub_pro(self):
        """Stub Pro entitlement unlocks project creation after an unentitled start."""
        owner = make_profile()
        client = Client()
        login_as(client, owner, grant_entitlement=False)

        blocked = client.post(
            reverse("surface:project-create"),
            project_form_data(title="Still Blocked"),
        )
        self.assertRedirects(blocked, reverse("surface:billing"))
        self.assertFalse(Project.objects.filter(title="Still Blocked").exists())

        client.post(reverse("surface:billing-activate-pro"))
        response = client.post(
            reverse("surface:project-create"),
            project_form_data(title="Created With Pro"),
        )

        project = Project.objects.get(title="Created With Pro")
        self.assertEqual(project.owner, owner)
        self.assertRedirects(
            response,
            reverse("surface:project-detail", kwargs={"project_pk": project.pk}),
        )

    @override_settings(**BILLING_STUB_SETTINGS)
    def test_project_create_allowed_after_stub_project_pack(self):
        """A project-pack credit unlocks one project and is consumed on create."""
        owner = make_profile()
        client = Client()
        login_as(client, owner, grant_entitlement=False)
        grant_session_entitlement(client, pack_credits=1)

        response = client.post(
            reverse("surface:project-create"),
            project_form_data(title="Created With Pack"),
        )

        project = Project.objects.get(title="Created With Pack")
        self.assertEqual(project.owner, owner)
        self.assertRedirects(
            response,
            reverse("surface:project-detail", kwargs={"project_pk": project.pk}),
        )
        billing_page = client.get(reverse("surface:billing"))
        self.assertContains(billing_page, "No project creation entitlement is active.")


class ChangeOrderCreateViewTests(TestCase):
    """Proposing change orders on active projects."""

    @override_settings(DEBUG=True, **LOCMem_EMAIL)
    def test_propose_change_order_on_active_emails_client_link(self):
        """An active project proposal emails a change-order review URL."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        advance_to_active(project)
        client = Client()
        login_as(client, owner)
        client.get(reverse("surface:project-detail", kwargs={"project_pk": project.pk}))

        response = client.post(
            reverse("surface:change-order-create", kwargs={"project_pk": project.pk}),
            change_order_form_data(),
            follow=True,
        )

        change_order = project.change_orders.get()
        self.assertEqual(change_order.status, ChangeOrder.Status.PROPOSED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/client/change-order/", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, [project.client_email])
        self.assertRedirects(
            response,
            reverse("surface:project-detail", kwargs={"project_pk": project.pk}),
        )
        self.assertContains(response, "Change order sent for client review.")

    @override_settings(**LOCMem_EMAIL)
    def test_change_order_email_keeps_action_first_and_discovers_portal(self):
        """The actual proposal view includes both action and portal URLs."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        advance_to_active(project)
        client = Client()
        login_as(client, owner)

        client.post(
            reverse("surface:change-order-create", kwargs={"project_pk": project.pk}),
            change_order_form_data(),
        )

        body_lines = [
            line for line in mail.outbox[0].body.splitlines() if line.strip()
        ]
        self.assertIn("/client/change-order/", body_lines[0])
        self.assertTrue(
            any("/portal/login/" in line for line in body_lines[1:]),
            "Change-order email did not include portal discovery after its action URL.",
        )

    def test_propose_change_order_returns_404_when_not_active(self):
        """Draft projects cannot propose change orders through the Surface UI."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        client = Client()
        login_as(client, owner)
        response = client.post(
            reverse("surface:change-order-create", kwargs={"project_pk": project.pk}),
            change_order_form_data(),
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(project.change_orders.exists())


class ChangeOrderTokenPurposeIsolationTests(TestCase):
    """Change-order tokens are isolated from review and sign purposes."""

    def setUp(self):
        """Active project with review, sign, and change-order tokens."""
        self.project = make_draft_project()
        advance_to_active(self.project)
        self.review_token = make_client_token(self.project, "review")
        self.sign_token = make_client_token(self.project, "sign")
        self.client = Client()

    def test_review_token_cannot_open_client_change_order_page(self):
        """A review-purpose token yields 410 on the change-order page."""
        response = self.client.get(
            reverse(
                "surface:client-change-order",
                kwargs={"token": self.review_token},
            )
        )
        self.assertEqual(response.status_code, 410)
        self.assertContains(
            response,
            "This review link is no longer available",
            status_code=410,
        )

    def test_sign_token_cannot_open_client_change_order_page(self):
        """A sign-purpose token yields 410 on the change-order page."""
        response = self.client.get(
            reverse(
                "surface:client-change-order",
                kwargs={"token": self.sign_token},
            )
        )
        self.assertEqual(response.status_code, 410)
        self.assertContains(
            response,
            "This review link is no longer available",
            status_code=410,
        )

    def test_change_order_token_cannot_open_client_review_page(self):
        """A change-order token cannot open criteria review."""
        change_order = self.project.change_orders.create(
            description="Scoped work",
            amount_cents=1000,
            timeline_days=2,
            status=ChangeOrder.Status.PROPOSED,
        )
        token = make_change_order_token(change_order)
        response = self.client.get(
            reverse("surface:client-review", kwargs={"token": token})
        )
        self.assertEqual(response.status_code, 410)


class ClientChangeOrderDecisionViewTests(TestCase):
    """Client approve/decline decisions via purpose-bound change-order tokens."""

    def setUp(self):
        """Proposed change order with a fresh client token."""
        self.project = make_draft_project()
        advance_to_active(self.project)
        self.change_order = self.project.change_orders.create(
            description="Add dashboard export",
            amount_cents=5000,
            timeline_days=3,
            status=ChangeOrder.Status.PROPOSED,
        )
        self.token = make_change_order_token(self.change_order)
        self.client = Client()

    def test_client_approve_via_token_sets_approved_status(self):
        """Approve POST resolves the change order to approved."""
        response = self.client.post(
            reverse("surface:client-change-order", kwargs={"token": self.token}),
            {"decision": "approve"},
        )
        self.change_order.refresh_from_db()
        self.assertEqual(self.change_order.status, ChangeOrder.Status.APPROVED)
        self.assertIsNotNone(self.change_order.resolved_at)
        self.assertEqual(response.status_code, 200)

    def test_client_decline_via_token_sets_declined_status(self):
        """Decline POST resolves the change order to declined."""
        response = self.client.post(
            reverse("surface:client-change-order", kwargs={"token": self.token}),
            {"decision": "decline"},
        )
        self.change_order.refresh_from_db()
        self.assertEqual(self.change_order.status, ChangeOrder.Status.DECLINED)
        self.assertIsNotNone(self.change_order.resolved_at)
        self.assertEqual(response.status_code, 200)


class MarkDeliveredChangeOrderGateTests(TestCase):
    """Delivery is blocked while proposed change orders remain open."""

    @override_settings(**LOCMem_EMAIL)
    def test_mark_delivered_blocked_while_proposed_change_order_open(self):
        """Open proposed change orders block delivery with a friendly message."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        advance_to_active(project)
        mark_all_items_passed(project)
        project.change_orders.create(
            description="Pending scope change",
            amount_cents=1000,
            timeline_days=1,
            status=ChangeOrder.Status.PROPOSED,
        )
        client = Client()
        login_as(client, owner)
        client.get(reverse("surface:project-detail", kwargs={"project_pk": project.pk}))

        response = client.post(
            reverse("surface:mark-delivered", kwargs={"project_pk": project.pk})
        )

        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.ACTIVE)
        self.assertEqual(len(mail.outbox), 0)
        follow = client.get(response.url)
        self.assertContains(
            follow,
            "Resolve all proposed change orders before delivery.",
        )

    @override_settings(**LOCMem_EMAIL)
    def test_mark_delivered_works_after_change_order_approved(self):
        """Delivery succeeds once the proposed change order is approved."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        advance_to_active(project)
        mark_all_items_passed(project)
        change_order = project.change_orders.create(
            description="Approved scope change",
            amount_cents=1000,
            timeline_days=1,
            status=ChangeOrder.Status.PROPOSED,
        )
        token = make_change_order_token(change_order)
        client = Client()
        client.post(
            reverse("surface:client-change-order", kwargs={"token": token}),
            {"decision": "approve"},
        )
        login_as(client, owner)
        client.get(reverse("surface:project-detail", kwargs={"project_pk": project.pk}))

        response = client.post(
            reverse("surface:mark-delivered", kwargs={"project_pk": project.pk}),
            follow=True,
        )

        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.DELIVERED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/client/sign/", mail.outbox[0].body)
        self.assertContains(response, "Delivery recorded and sent for signature.")

    @override_settings(**LOCMem_EMAIL)
    def test_mark_delivered_works_after_change_order_declined(self):
        """Delivery succeeds once the proposed change order is declined."""
        owner = make_profile()
        project = make_draft_project(owner=owner)
        advance_to_active(project)
        mark_all_items_passed(project)
        change_order = project.change_orders.create(
            description="Declined scope change",
            amount_cents=1000,
            timeline_days=1,
            status=ChangeOrder.Status.PROPOSED,
        )
        token = make_change_order_token(change_order)
        client = Client()
        client.post(
            reverse("surface:client-change-order", kwargs={"token": token}),
            {"decision": "decline"},
        )
        login_as(client, owner)
        client.get(reverse("surface:project-detail", kwargs={"project_pk": project.pk}))

        response = client.post(
            reverse("surface:mark-delivered", kwargs={"project_pk": project.pk}),
            follow=True,
        )

        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.DELIVERED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertContains(response, "Delivery recorded and sent for signature.")


class AiDraftViewTests(TestCase):
    """AI draft generate/confirm stays preview-only until explicit human confirm."""

    def setUp(self):
        """One editable draft project owned by the authenticated freelancer."""
        self.owner = make_profile()
        self.project = make_draft_project(owner=self.owner)
        self.original_brief = self.project.brief
        self.client = Client()
        login_as(self.client, self.owner)

    def test_generate_does_not_persist_brief_or_criteria(self):
        """Generate shows a preview without writing brief or acceptance items."""
        item_count_before = self.project.acceptance_items.count()
        response = self.client.post(
            reverse(
                "surface:ai-draft-generate",
                kwargs={"project_pk": self.project.pk},
            ),
            ai_dump_form_data(),
        )
        self.project.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.project.brief, self.original_brief)
        self.assertEqual(self.project.acceptance_items.count(), item_count_before)

    def test_generate_shows_editable_preview(self):
        """Generate renders the confirm form with draft fields for human review."""
        response = self.client.post(
            reverse(
                "surface:ai-draft-generate",
                kwargs={"project_pk": self.project.pk},
            ),
            ai_dump_form_data(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Review and edit the draft below")
        self.assertContains(response, "Confirm and apply draft")
        self.assertContains(response, "Export CSV")
        self.assertContains(response, "Filter by date range")

    def test_confirm_applies_brief_and_replaces_criteria(self):
        """Confirm persists the edited brief and replaces acceptance criteria."""
        self.client.post(
            reverse(
                "surface:ai-draft-generate",
                kwargs={"project_pk": self.project.pk},
            ),
            ai_dump_form_data(),
        )
        response = self.client.post(
            reverse(
                "surface:ai-draft-confirm",
                kwargs={"project_pk": self.project.pk},
            ),
            ai_confirm_form_data(
                brief="Confirmed brief text",
                criteria_text="Applied criterion A\nApplied criterion B",
            ),
        )
        self.project.refresh_from_db()
        self.assertRedirects(
            response,
            reverse("surface:project-detail", kwargs={"project_pk": self.project.pk}),
        )
        self.assertEqual(self.project.brief, "Confirmed brief text")
        texts = list(
            self.project.acceptance_items.order_by("order").values_list("text", flat=True)
        )
        self.assertEqual(texts, ["Applied criterion A", "Applied criterion B"])

    def test_confirm_replacing_draft_criteria_cascades_their_steps(self):
        """Human-confirmed bulk replacement removes steps under deleted drafts."""
        old_item = self.project.acceptance_items.get()
        old_step = AcceptanceStep.objects.create(
            item=old_item,
            text="Step beneath replaced AI scope",
            order=1,
        )

        response = self.client.post(
            reverse(
                "surface:ai-draft-confirm",
                kwargs={"project_pk": self.project.pk},
            ),
            ai_confirm_form_data(),
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(AcceptanceItem.objects.filter(pk=old_item.pk).exists())
        self.assertFalse(AcceptanceStep.objects.filter(pk=old_step.pk).exists())

    def test_confirm_without_valid_form_leaves_project_unchanged(self):
        """Invalid confirm POST re-renders preview and leaves stored project state intact."""
        item = self.project.acceptance_items.get()
        response = self.client.post(
            reverse(
                "surface:ai-draft-confirm",
                kwargs={"project_pk": self.project.pk},
            ),
            {"brief": "", "criteria_text": ""},
        )
        self.project.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required")
        self.assertEqual(self.project.brief, self.original_brief)
        self.assertEqual(self.project.acceptance_items.count(), 1)
        item.refresh_from_db()
        self.assertEqual(item.text, "First criterion")

    def test_ai_draft_endpoints_return_404_on_active_project(self):
        """Generate and confirm are unavailable once the project leaves draft."""
        advance_to_active(self.project)
        generate = self.client.post(
            reverse(
                "surface:ai-draft-generate",
                kwargs={"project_pk": self.project.pk},
            ),
            ai_dump_form_data(),
        )
        confirm = self.client.post(
            reverse(
                "surface:ai-draft-confirm",
                kwargs={"project_pk": self.project.pk},
            ),
            ai_confirm_form_data(),
        )
        self.assertEqual(generate.status_code, 404)
        self.assertEqual(confirm.status_code, 404)

    def test_non_owner_cannot_generate_or_confirm_ai_draft(self):
        """Another freelancer cannot run AI draft actions on an owned project."""
        other = make_profile(handle="ai-other")
        other_client = Client()
        login_as(other_client, other)
        generate = other_client.post(
            reverse(
                "surface:ai-draft-generate",
                kwargs={"project_pk": self.project.pk},
            ),
            ai_dump_form_data(),
        )
        confirm = other_client.post(
            reverse(
                "surface:ai-draft-confirm",
                kwargs={"project_pk": self.project.pk},
            ),
            ai_confirm_form_data(),
        )
        self.assertEqual(generate.status_code, 404)
        self.assertEqual(confirm.status_code, 404)
        self.project.refresh_from_db()
        self.assertEqual(self.project.brief, self.original_brief)


class BillingPageTests(TestCase):
    """Stub billing controls grant session entitlement."""

    @override_settings(**BILLING_STUB_SETTINGS)
    def test_billing_activate_pro_sets_entitlement(self):
        """Activate Pro stores subscription entitlement in the session."""
        owner = make_profile()
        client = Client()
        login_as(client, owner, grant_entitlement=False)

        response = client.post(
            reverse("surface:billing-activate-pro"),
            follow=True,
        )

        self.assertRedirects(response, reverse("surface:billing"))
        self.assertContains(response, "Pro is active for this session.")
        self.assertContains(response, "Billing entitlement activated.")

    @override_settings(**BILLING_STUB_SETTINGS)
    def test_billing_buy_project_pack_sets_entitlement(self):
        """Buy project pack adds one credit to the session."""
        owner = make_profile()
        client = Client()
        login_as(client, owner, grant_entitlement=False)

        response = client.post(
            reverse("surface:billing-buy-project-pack"),
            follow=True,
        )

        self.assertRedirects(response, reverse("surface:billing"))
        self.assertContains(response, "1 project pack credit available.")
        self.assertContains(response, "Billing entitlement activated.")

    @override_settings(ATTEST_BILLING_STUB_MODE=False)
    def test_billing_stub_disabled_refuses_activate_pro(self):
        """Stub grant endpoints do nothing when ATTEST_BILLING_STUB_MODE is off."""
        owner = make_profile()
        client = Client()
        login_as(client, owner, grant_entitlement=False)

        response = client.post(
            reverse("surface:billing-activate-pro"),
            follow=True,
        )

        self.assertRedirects(response, reverse("surface:billing"))
        self.assertContains(response, "Local billing controls are not available.")
        blocked = client.post(
            reverse("surface:project-create"),
            project_form_data(title="No Stub Create"),
        )
        self.assertRedirects(blocked, reverse("surface:billing"))
        self.assertFalse(Project.objects.filter(title="No Stub Create").exists())


class ClientPortalFoundationTests(TestCase):
    """Account-free portal authentication, isolation, and project discovery."""

    def setUp(self):
        """Clear portal counters and create one client-visible project."""
        cache.clear()
        self.email = "client@example.com"
        self.owner = make_profile(
            handle="portal-owner",
            email="freelancer-owner@example.com",
        )
        self.owner.display_name = "Portal Freelancer"
        self.owner.save(update_fields=["display_name"])
        self.project = make_draft_project(owner=self.owner)
        self.project.client_email = "Client@Example.COM"
        self.project.save(update_fields=["client_email"])
        submit_criteria_for_approval(self.project)
        self.project.refresh_from_db()

    def establish_portal_session(self, client, email=None):
        """Store a current verified client identity in a test session."""
        session = client.session
        session[client_auth.CLIENT_EMAIL_SESSION_KEY] = email or self.email
        session[client_auth.CLIENT_VERIFIED_AT_SESSION_KEY] = (
            timezone.now().timestamp()
        )
        session.save()

    @override_settings(**LOCMem_EMAIL)
    def test_complete_portal_cycle_creates_no_user_or_profile(self):
        """Every portal route preserves the User and Profile row counts."""
        existing_identity_email = self.owner.user.email
        self.project.client_email = existing_identity_email
        self.project.save(update_fields=["client_email"])
        self.assertTrue(
            get_user_model().objects.filter(email=existing_identity_email).exists()
        )
        self.assertTrue(Profile.objects.filter(user=self.owner.user).exists())
        user_count = get_user_model().objects.count()
        profile_count = Profile.objects.count()
        client = Client()

        def assert_identity_counts_unchanged(route_name):
            """Assert neither account-backed identity table changed."""
            self.assertEqual(
                Profile.objects.count(),
                profile_count,
                f"Profile count changed after {route_name}",
            )
            self.assertEqual(
                get_user_model().objects.count(),
                user_count,
                f"User count changed after {route_name}",
            )

        request_get_response = client.get(reverse("surface:portal-request"))
        self.assertEqual(request_get_response.status_code, 200)
        assert_identity_counts_unchanged("portal-request GET")

        request_post_response = client.post(
            reverse("surface:portal-request"),
            {"email": existing_identity_email},
        )
        self.assertRedirects(
            request_post_response,
            reverse("surface:portal-sent"),
            fetch_redirect_response=False,
        )
        assert_identity_counts_unchanged("portal-request POST")
        self.assertEqual(len(mail.outbox), 1)
        portal_path = urlparse(mail.outbox[0].body.splitlines()[0])
        confirm_path = f"{portal_path.path}?{portal_path.query}"

        sent_response = client.get(reverse("surface:portal-sent"))
        self.assertEqual(sent_response.status_code, 200)
        assert_identity_counts_unchanged("portal-sent GET")

        login_get_response = client.get(confirm_path)
        self.assertContains(login_get_response, "Confirm your client portal access")
        assert_identity_counts_unchanged("portal-login GET")

        login_post_response = client.post(
            reverse("surface:portal-login"),
            {"token": login_get_response.context["token"]},
        )
        self.assertRedirects(
            login_post_response,
            reverse("surface:portal"),
            fetch_redirect_response=False,
        )
        assert_identity_counts_unchanged("portal-login POST")
        self.assertEqual(
            client.session[client_auth.CLIENT_EMAIL_SESSION_KEY],
            existing_identity_email,
        )

        portal_response = client.get(reverse("surface:portal"))
        self.assertEqual(portal_response.status_code, 200)
        assert_identity_counts_unchanged("portal list GET")

        detail_response = client.get(
            reverse(
                "surface:portal-project",
                kwargs={"project_pk": self.project.pk},
            )
        )
        self.assertEqual(detail_response.status_code, 200)
        assert_identity_counts_unchanged("portal project GET")

        logout_response = client.post(reverse("surface:portal-logout"))
        self.assertRedirects(
            logout_response,
            reverse("surface:portal-request"),
            fetch_redirect_response=False,
        )
        assert_identity_counts_unchanged("portal-logout POST")

    def test_virgin_client_detail_get_creates_no_identity_rows(self):
        """A detail route cannot mint identity rows for a brand-new client email."""
        virgin_email = "virgin-portal-detail@example.com"
        self.assertFalse(
            get_user_model().objects.filter(email__iexact=virgin_email).exists()
        )
        virgin_project = make_draft_project(owner=self.owner)
        virgin_project.client_email = virgin_email
        virgin_project.save(update_fields=["client_email"])
        submit_criteria_for_approval(virgin_project)
        client = Client()
        session = client.session
        session[client_auth.CLIENT_EMAIL_SESSION_KEY] = virgin_email
        session[client_auth.CLIENT_VERIFIED_AT_SESSION_KEY] = (
            timezone.now().timestamp()
        )
        session.save()
        user_count = get_user_model().objects.count()
        profile_count = Profile.objects.count()

        response = client.get(
            reverse(
                "surface:portal-project",
                kwargs={"project_pk": virgin_project.pk},
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_user_model().objects.count(), user_count)
        self.assertEqual(Profile.objects.count(), profile_count)
        self.assertFalse(
            get_user_model().objects.filter(email__iexact=virgin_email).exists()
        )

    @override_settings(**LOCMem_EMAIL)
    def test_portal_routes_never_reach_freelancer_identity_functions(self):
        """Every portal endpoint remains independent of freelancer identity creation."""
        token = make_portal_token(self.email)
        client = Client()

        with (
            patch("surface.auth.get_or_create_freelancer") as create_freelancer,
            patch("surface.auth.ensure_profile") as ensure_freelancer_profile,
        ):
            request_get = client.get(reverse("surface:portal-request"))
            request_post = client.post(
                reverse("surface:portal-request"),
                {"email": self.email},
            )
            sent_get = client.get(reverse("surface:portal-sent"))
            login_get = client.get(
                reverse("surface:portal-login"),
                {"token": token},
            )
            login_post = client.post(
                reverse("surface:portal-login"),
                {"token": token},
            )
            portal_get = client.get(reverse("surface:portal"))
            detail_get = client.get(
                reverse(
                    "surface:portal-project",
                    kwargs={"project_pk": self.project.pk},
                )
            )
            logout_post = client.post(reverse("surface:portal-logout"))

        self.assertEqual(request_get.status_code, 200)
        self.assertEqual(request_post.status_code, 302)
        self.assertEqual(request_post.url, reverse("surface:portal-sent"))
        self.assertEqual(sent_get.status_code, 200)
        self.assertEqual(login_get.status_code, 200)
        self.assertEqual(login_post.status_code, 302)
        self.assertEqual(login_post.url, reverse("surface:portal"))
        self.assertEqual(portal_get.status_code, 200)
        self.assertEqual(detail_get.status_code, 200)
        self.assertEqual(logout_post.status_code, 302)
        self.assertEqual(logout_post.url, reverse("surface:portal-request"))
        create_freelancer.assert_not_called()
        ensure_freelancer_profile.assert_not_called()

    def test_client_auth_has_no_forbidden_identity_imports(self):
        """Portal auth and display modules cannot import freelancer identity code."""
        for module in (client_auth, portal_views):
            with self.subTest(module=module.__name__):
                module_tree = ast.parse(inspect.getsource(module))
                imported_names = {
                    alias.name
                    for node in ast.walk(module_tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names
                }
                directly_imported_modules = {
                    alias.name
                    for node in ast.walk(module_tree)
                    if isinstance(node, ast.Import)
                    for alias in node.names
                }
                dynamic_import_calls = [
                    node
                    for node in ast.walk(module_tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "__import__"
                ]
                forbidden_dynamic_module_calls = [
                    node
                    for node in ast.walk(module_tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "importlib"
                    and node.func.attr == "import_module"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value
                    in {"django.contrib.auth", "surface.auth"}
                ]

                self.assertNotIn("get_or_create_freelancer", imported_names)
                self.assertNotIn("ensure_profile", imported_names)
                self.assertNotIn("Profile", imported_names)
                self.assertNotIn("importlib", imported_names)
                self.assertNotIn("import_module", imported_names)
                self.assertNotIn("ledger.models", directly_imported_modules)
                self.assertEqual(dynamic_import_calls, [])
                self.assertEqual(forbidden_dynamic_module_calls, [])

    def test_portal_and_project_token_namespaces_are_isolated_both_ways(self):
        """No portal credential is accepted by any project-scoped purpose."""
        portal_token = make_portal_token(self.email)
        for purpose in ("review", "sign", "change_order"):
            with self.subTest(purpose=purpose):
                with self.assertRaises(signing.BadSignature):
                    read_client_token(portal_token, purpose)

        for purpose in ("review", "sign"):
            with self.subTest(purpose=purpose):
                with self.assertRaises(signing.BadSignature):
                    read_portal_token(make_client_token(self.project, purpose))
        change_order = ChangeOrder.objects.create(
            project=self.project,
            description="Portal isolation",
            amount_cents=100,
            timeline_days=1,
        )
        with self.assertRaises(signing.BadSignature):
            read_portal_token(make_change_order_token(change_order))

    def test_portal_token_salt_differs_from_every_project_purpose(self):
        """The portal signer has a distinct namespace independent of payload shape."""
        project_salts = {
            f"surface.client.{purpose}" for purpose in CLIENT_TOKEN_PURPOSES
        }
        self.assertNotIn(PORTAL_TOKEN_SALT, project_salts)

    def test_portal_token_rejects_invalid_nonce_shape(self):
        """A portal-signed payload still requires a random 32-character nonce."""
        token = signing.dumps(
            {"email": self.email, "nonce": "too-short"},
            salt=PORTAL_TOKEN_SALT,
            compress=True,
        )

        with self.assertRaises(signing.BadSignature):
            read_portal_token(token)

    def test_portal_token_rejects_non_normalized_email(self):
        """A portal-signed payload cannot carry mixed-case or padded identity."""
        token = signing.dumps(
            {"email": "Client@Example.COM", "nonce": "a" * 32},
            salt=PORTAL_TOKEN_SALT,
            compress=True,
        )

        with self.assertRaises(signing.BadSignature):
            read_portal_token(token)

    def test_portal_used_token_namespace_is_distinct_and_pinned(self):
        """Portal consumption markers never share the freelancer namespace."""
        self.assertEqual(
            client_auth.PORTAL_LOGIN_USED_NAMESPACE,
            "surface.portal-login.used",
        )
        self.assertNotEqual(
            client_auth.PORTAL_LOGIN_USED_NAMESPACE,
            MAGIC_LOGIN_USED_NAMESPACE,
        )

    def test_get_does_not_consume_and_post_consumes_portal_token_once(self):
        """Scanner GETs are harmless and only the first confirmation POST succeeds."""
        token = make_portal_token(self.email)
        login_url = reverse("surface:portal-login")

        get_response = Client().get(login_url, {"token": token})
        self.assertContains(get_response, "Confirm your client portal access")

        first_client = Client()
        first_response = first_client.post(login_url, {"token": token})
        self.assertRedirects(first_response, reverse("surface:portal"))

        second_client = Client()
        second_response = second_client.post(login_url, {"token": token})
        self.assertContains(second_response, "no longer available")
        self.assertNotIn(
            client_auth.CLIENT_EMAIL_SESSION_KEY,
            second_client.session,
        )

    def test_expired_portal_token_is_rejected(self):
        """A portal link stops working after its one-hour lifetime."""
        base_time = 1_700_000_000
        with patch("django.core.signing.time.time", return_value=base_time):
            token = make_portal_token(self.email)
        with patch(
            "django.core.signing.time.time",
            return_value=base_time + PORTAL_TOKEN_MAX_AGE + 1,
        ):
            response = Client().get(
                reverse("surface:portal-login"),
                {"token": token},
            )
        self.assertContains(response, "no longer available")

    @override_settings(**LOCMem_EMAIL)
    def test_new_portal_link_after_consumption_is_usable(self):
        """A consumed link does not prevent a fresh request and confirmation."""
        first_token = make_portal_token(self.email)
        client_auth.read_and_consume_portal_token(first_token)

        request_client = Client()
        request_client.post(
            reverse("surface:portal-request"),
            {"email": self.email},
        )
        parsed = urlparse(mail.outbox[0].body.splitlines()[0])
        query = parsed.query
        second_token = query.partition("token=")[2]
        from urllib.parse import unquote

        second_token = unquote(second_token)
        self.assertNotEqual(second_token, first_token)
        self.assertEqual(
            client_auth.read_and_consume_portal_token(second_token),
            self.email,
        )

    @override_settings(DEBUG=False, **LOCMem_EMAIL)
    def test_unknown_draft_only_and_limited_requests_have_enumeration_parity(self):
        """Every valid request has identical visible status, messages, and sent page."""
        draft = make_draft_project(owner=self.owner)
        draft.client_email = "draft-only@example.com"
        draft.save(update_fields=["client_email"])
        limited_project = make_draft_project(owner=self.owner)
        limited_project.client_email = "limited-client@example.com"
        limited_project.save(update_fields=["client_email"])
        submit_criteria_for_approval(limited_project)
        for _request_number in range(client_auth.PORTAL_REQUEST_LIMIT):
            client_auth.portal_link_request_allowed(limited_project.client_email)

        def visible_outcome(email):
            """Return all user-visible channels for one portal request."""
            request_client = Client()
            response = request_client.post(
                reverse("surface:portal-request"),
                {"email": email},
            )
            visible_messages = tuple(
                (message.level, message.message, message.tags)
                for message in get_messages(response.wsgi_request)
            )
            sent_response = request_client.get(reverse("surface:portal-sent"))
            return (
                response.status_code,
                response.url,
                visible_messages,
                sent_response.status_code,
                sent_response.content,
            )

        unknown_outcome = visible_outcome("unknown@example.com")
        draft_outcome = visible_outcome(draft.client_email)
        limited_outcome = visible_outcome(limited_project.client_email)
        successful_outcome = visible_outcome(self.email)

        self.assertEqual(unknown_outcome, successful_outcome)
        self.assertEqual(draft_outcome, successful_outcome)
        self.assertEqual(limited_outcome, successful_outcome)
        self.assertEqual(len(mail.outbox), 1)

    def test_server_timestamp_expires_session_while_cookie_remains_valid(self):
        """Absolute client expiry does not rely on Django's sliding cookie age."""
        client = Client()
        session = client.session
        session[client_auth.CLIENT_EMAIL_SESSION_KEY] = self.email
        session[client_auth.CLIENT_VERIFIED_AT_SESSION_KEY] = (
            timezone.now().timestamp() - client_auth.CLIENT_SESSION_MAX_AGE - 1
        )
        session["unrelated_session_value"] = "preserved"
        session.save()

        response = client.get(reverse("surface:portal"))

        self.assertRedirects(
            response,
            reverse("surface:portal-request"),
            fetch_redirect_response=False,
        )
        self.assertNotIn(client_auth.CLIENT_EMAIL_SESSION_KEY, client.session)
        self.assertNotIn(client_auth.CLIENT_VERIFIED_AT_SESSION_KEY, client.session)
        self.assertEqual(client.session["unrelated_session_value"], "preserved")

    def test_successful_reads_do_not_slide_absolute_session_cutoff(self):
        """Activity preserves verification time and cannot extend the original cutoff."""
        client = Client()
        current_time = timezone.now()
        original_verified_at = (
            current_time.timestamp() - client_auth.CLIENT_SESSION_MAX_AGE + 60
        )
        session = client.session
        session[client_auth.CLIENT_EMAIL_SESSION_KEY] = self.email
        session[client_auth.CLIENT_VERIFIED_AT_SESSION_KEY] = original_verified_at
        session.save()

        with patch("surface.client_auth.timezone.now", return_value=current_time):
            active_response = client.get(reverse("surface:portal"))

        self.assertEqual(active_response.status_code, 200)
        self.assertEqual(
            client.session[client_auth.CLIENT_VERIFIED_AT_SESSION_KEY],
            original_verified_at,
        )

        after_original_cutoff = current_time + timedelta(seconds=61)
        with patch(
            "surface.client_auth.timezone.now",
            return_value=after_original_cutoff,
        ):
            expired_response = client.get(reverse("surface:portal"))

        self.assertEqual(expired_response.status_code, 302)
        self.assertEqual(expired_response.url, reverse("surface:portal-request"))
        self.assertNotIn(client_auth.CLIENT_EMAIL_SESSION_KEY, client.session)
        self.assertNotIn(client_auth.CLIENT_VERIFIED_AT_SESSION_KEY, client.session)

    def test_expired_client_identity_is_hidden_without_context_mutation(self):
        """The header hides an expired client while pure context lookup preserves it."""
        client = Client()
        session = client.session
        session[client_auth.CLIENT_EMAIL_SESSION_KEY] = self.email
        session[client_auth.CLIENT_VERIFIED_AT_SESSION_KEY] = (
            timezone.now().timestamp() - client_auth.CLIENT_SESSION_MAX_AGE - 1
        )
        session.save()

        response = client.get(reverse("surface:portal-request"))

        self.assertNotContains(response, 'data-identity="client"')
        self.assertNotContains(response, self.email)
        self.assertEqual(
            client.session[client_auth.CLIENT_EMAIL_SESSION_KEY],
            self.email,
        )
        self.assertIn(client_auth.CLIENT_VERIFIED_AT_SESSION_KEY, client.session)

    def test_portal_lists_case_insensitive_matches_without_drafts(self):
        """A client sees all and only matching non-draft projects across owners."""
        other_owner = make_profile(handle="second-portal-owner")
        second_project = make_draft_project(owner=other_owner)
        second_project.title = "Second freelancer project"
        second_project.client_email = self.email
        second_project.save(update_fields=["title", "client_email"])
        submit_criteria_for_approval(second_project)
        hidden_draft = make_draft_project(owner=other_owner)
        hidden_draft.title = "Private draft"
        hidden_draft.client_email = self.email
        hidden_draft.save(update_fields=["title", "client_email"])
        foreign_project = make_draft_project(owner=other_owner)
        foreign_project.title = "Different client"
        foreign_project.client_email = "other@example.com"
        foreign_project.save(update_fields=["title", "client_email"])
        submit_criteria_for_approval(foreign_project)
        client = Client()
        self.establish_portal_session(client, "CLIENT@EXAMPLE.COM")

        response = client.get(reverse("surface:portal"))

        self.assertContains(response, self.project.title)
        self.assertContains(response, "Portal Freelancer")
        self.assertContains(response, second_project.title)
        self.assertNotContains(response, hidden_draft.title)
        self.assertNotContains(response, foreign_project.title)

    def test_client_sign_out_preserves_freelancer_and_billing_session(self):
        """Client sign-out removes two client keys and leaves the other hat intact."""
        client = Client()
        login_as(client, self.owner)
        self.establish_portal_session(client)

        response = client.post(reverse("surface:portal-logout"))

        self.assertRedirects(response, reverse("surface:portal-request"))
        self.assertIn("_auth_user_id", client.session)
        self.assertTrue(client.session[billing._PRO_SESSION_KEY])
        self.assertNotIn(client_auth.CLIENT_EMAIL_SESSION_KEY, client.session)
        self.assertNotIn(client_auth.CLIENT_VERIFIED_AT_SESSION_KEY, client.session)

    def test_freelancer_logout_flushes_client_session(self):
        """Freelancer logout intentionally ends both identities in one browser."""
        client = Client()
        login_as(client, self.owner)
        self.establish_portal_session(client)

        response = client.post(reverse("surface:logout"))

        self.assertRedirects(response, reverse("surface:login-request"))
        self.assertNotIn("_auth_user_id", client.session)
        self.assertNotIn(client_auth.CLIENT_EMAIL_SESSION_KEY, client.session)

    def test_switching_freelancers_flushes_existing_client_session(self):
        """Django deliberately flushes client state when login changes user identity."""
        client = Client()
        self.establish_portal_session(client)
        original_verified_at = client.session[
            client_auth.CLIENT_VERIFIED_AT_SESSION_KEY
        ]
        first_token = make_magic_login_token(self.owner.user)

        first_response = client.post(
            reverse("surface:magic-login"),
            {"token": first_token},
        )

        self.assertRedirects(
            first_response,
            reverse("surface:project-list"),
            fetch_redirect_response=False,
        )
        self.assertEqual(int(client.session["_auth_user_id"]), self.owner.user_id)
        self.assertEqual(
            client.session[client_auth.CLIENT_EMAIL_SESSION_KEY],
            self.email,
        )
        self.assertEqual(
            client.session[client_auth.CLIENT_VERIFIED_AT_SESSION_KEY],
            original_verified_at,
        )

        second_owner = make_profile(
            handle="second-login-owner",
            email="second-login-owner@example.com",
        )
        second_token = make_magic_login_token(second_owner.user)
        second_response = client.post(
            reverse("surface:magic-login"),
            {"token": second_token},
        )

        self.assertRedirects(
            second_response,
            reverse("surface:project-list"),
            fetch_redirect_response=False,
        )
        self.assertEqual(int(client.session["_auth_user_id"]), second_owner.user_id)
        self.assertNotIn(client_auth.CLIENT_EMAIL_SESSION_KEY, client.session)
        self.assertNotIn(client_auth.CLIENT_VERIFIED_AT_SESSION_KEY, client.session)

    def test_two_identity_strips_render_distinctly_with_both_emails(self):
        """The two-hats header labels freelancer and client identity separately."""
        client = Client()
        login_as(client, self.owner)
        self.establish_portal_session(client)

        response = client.get(reverse("surface:portal"))

        self.assertContains(response, 'data-identity="freelancer"')
        self.assertContains(response, 'data-identity="client"')
        self.assertContains(response, self.owner.user.email)
        self.assertContains(response, self.email)

    def test_portal_login_rotates_session_without_losing_freelancer(self):
        """Client verification closes fixation while retaining an authenticated user."""
        client = Client()
        login_as(client, self.owner)
        old_session_key = client.session.session_key
        token = make_portal_token(self.email)

        response = client.post(
            reverse("surface:portal-login"),
            {"token": token},
        )

        self.assertRedirects(response, reverse("surface:portal"))
        self.assertNotEqual(client.session.session_key, old_session_key)
        self.assertEqual(int(client.session["_auth_user_id"]), self.owner.user_id)

    def test_live_project_client_email_cannot_be_edited(self):
        """The portal identity dependency fails if live project edits are widened."""
        client = Client()
        login_as(client, self.owner)
        original_email = self.project.client_email

        response = client.post(
            reverse(
                "surface:project-update",
                kwargs={"project_pk": self.project.pk},
            ),
            project_form_data(client_email="intruder@example.com"),
        )

        self.project.refresh_from_db()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.project.client_email, original_email)

    def test_action_email_body_keeps_action_first_and_adds_portal_discovery(self):
        """All three action email types append the plain portal request URL."""
        from surface.views import _with_portal_discovery

        request = RequestFactory().get("/")
        request.META["HTTP_HOST"] = "testserver"
        action_url = "http://testserver/client/review/action-token/"

        body = _with_portal_discovery(request, action_url)

        self.assertEqual(body.splitlines()[0], action_url)
        self.assertIn("http://testserver/portal/login/", body.splitlines()[-1])

    @override_settings(
        DEBUG=True,
        EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend",
    )
    def test_console_portal_request_exposes_one_time_dev_link(self):
        """DEBUG console delivery provides a one-click QP-safe portal URL."""
        client = Client()
        response = client.post(
            reverse("surface:portal-request"),
            {"email": self.email},
            follow=True,
        )

        self.assertContains(response, "Continue to client portal")
        self.assertContains(response, "/portal/login/confirm/?token=")
        second_response = client.get(reverse("surface:portal-sent"))
        self.assertNotContains(second_response, "Continue to client portal")

    @override_settings(DEBUG=True)
    def test_quoted_printable_portal_token_is_repaired_in_debug(self):
        """Portal confirmation shares the console token repair primitive."""
        token = make_portal_token(self.email)
        mangled = "3D" + token[:20] + "=" + token[20:]

        response = Client().get(
            reverse("surface:portal-login"),
            {"token": mangled},
        )

        self.assertContains(response, "Confirm your client portal access")


class ClientPortalProjectViewTests(TestCase):
    """Read-only client project detail, progress, and signed-record replay."""

    def setUp(self):
        """Create one submitted project and a verified matching client session."""
        self.owner = make_profile(handle="portal-detail-owner")
        self.project = make_draft_project(owner=self.owner)
        self.project.client_email = "portal-detail@example.com"
        self.project.brief = "Client-readable project brief"
        self.project.save(update_fields=["client_email", "brief"])
        submit_criteria_for_approval(self.project)
        self.project.refresh_from_db()
        self.client = Client()
        session = self.client.session
        session[client_auth.CLIENT_EMAIL_SESSION_KEY] = "PORTAL-DETAIL@example.com"
        session[client_auth.CLIENT_VERIFIED_AT_SESSION_KEY] = (
            timezone.now().timestamp()
        )
        session.save()

    def detail_url(self, project=None):
        """Return the portal detail URL for one project."""
        return reverse(
            "surface:portal-project",
            kwargs={"project_pk": (project or self.project).pk},
        )

    @staticmethod
    def portal_detail_markup(response):
        """Return only the read-only project article, excluding global navigation."""
        content = response.content.decode()
        start = content.index('<article id="portal-project-detail">')
        end = content.index("</article>", start) + len("</article>")
        return content[start:end]

    @staticmethod
    def signed_record_markup(response):
        """Return only the frozen signed-record section."""
        return response.content.decode().split(
            '<section class="card signed-record" data-signed-record>',
            1,
        )[1].split("</section>", 1)[0]

    @staticmethod
    def delivery_item_markup_from_content(content, item_text):
        """Return shared delivery-row markup surrounding one criterion."""
        item_position = content.index(item_text)
        start = content.rfind("<li data-delivery-item>", 0, item_position)
        end = content.index("</li>", item_position) + len("</li>")
        return content[start:end]

    @classmethod
    def delivery_item_markup(cls, response, item_text):
        """Return shared delivery-row markup from a full response."""
        return cls.delivery_item_markup_from_content(
            response.content.decode(),
            item_text,
        )

    @staticmethod
    def status_badge_text(markup):
        """Extract exactly one client-facing status badge's text."""
        match = re.search(r'<span class="badge(?: failed)?">([^<]+)</span>', markup)
        if match is None:
            raise AssertionError("Client-facing status badge was not rendered.")
        return match.group(1).strip()

    def assert_checklist_mode(self, response, *, live, signed):
        """Assert exactly the authoritative checklist mode expected for a status."""
        detail_markup = self.portal_detail_markup(response)
        live_count = detail_markup.count("<h2>Scope and progress</h2>")
        signed_count = detail_markup.count("<h2>Signed delivery record</h2>")
        self.assertEqual(live_count, int(live))
        self.assertEqual(signed_count, int(signed))
        self.assertEqual(live_count + signed_count, 1)

    def test_other_client_and_draft_projects_return_404(self):
        """Project existence is hidden from foreign clients and for private drafts."""
        foreign_project = make_draft_project(owner=self.owner)
        foreign_project.client_email = "someone-else@example.com"
        foreign_project.save(update_fields=["client_email"])
        submit_criteria_for_approval(foreign_project)
        private_draft = make_draft_project(owner=self.owner)
        private_draft.client_email = "portal-detail@example.com"
        private_draft.save(update_fields=["client_email"])

        foreign_response = self.client.get(self.detail_url(foreign_project))
        draft_response = self.client.get(self.detail_url(private_draft))

        self.assertEqual(foreign_response.status_code, 404)
        self.assertEqual(draft_response.status_code, 404)

    def test_all_client_statuses_use_consistent_plain_language(self):
        """List and detail render every visible status with the same client wording."""
        expected_labels = {
            Project.Status.CRITERIA_PENDING: "Awaiting your approval",
            Project.Status.ACTIVE: "Work in progress",
            Project.Status.DELIVERED: "Delivered — awaiting your signature",
            Project.Status.ATTESTED: "Signed",
            Project.Status.DISPUTED: "Disputed",
        }
        for status, label in expected_labels.items():
            with self.subTest(status=status):
                Project.objects.filter(pk=self.project.pk).update(status=status)
                detail_response = self.client.get(self.detail_url())
                list_response = self.client.get(reverse("surface:portal"))

                self.assertEqual(detail_response.status_code, 200)
                detail_badge = self.status_badge_text(
                    self.portal_detail_markup(detail_response)
                )
                list_content = list_response.content.decode()
                row_start = list_content.index('<a class="portal-project-row"')
                row_end = list_content.index("</a>", row_start)
                list_badge = self.status_badge_text(
                    list_content[row_start:row_end]
                )
                self.assertEqual(detail_badge, label)
                self.assertEqual(list_badge, label)
                self.assertEqual(detail_badge, list_badge)

    def test_detail_shows_scope_live_steps_and_change_order_history(self):
        """Active detail exposes client-visible scope, progress, and prior changes."""
        approve_criteria(self.project)
        self.project.refresh_from_db()
        item = self.project.acceptance_items.get()
        AcceptanceStep.objects.create(
            item=item,
            text="Completed granular step",
            order=1,
            is_done=True,
        )
        AcceptanceStep.objects.create(
            item=item,
            text="Outstanding granular step",
            order=2,
            is_done=False,
        )
        AcceptanceItem.objects.create(
            project=self.project,
            text="Private freelancer draft criterion",
            order=2,
            state=AcceptanceItem.State.DRAFT,
        )
        ChangeOrder.objects.create(
            project=self.project,
            description="Approved reporting extension",
            amount_cents=12000,
            timeline_days=3,
            status=ChangeOrder.Status.APPROVED,
            resolved_at=timezone.now(),
        )

        response = self.client.get(self.detail_url())

        self.assertContains(response, self.project.brief)
        self.assertContains(response, item.text)
        self.assertContains(response, "Completed granular step")
        self.assertContains(response, "Outstanding granular step")
        self.assertContains(response, "Done")
        self.assertContains(response, "Not done")
        self.assertContains(response, "Approved reporting extension")
        self.assertContains(response, "Approved")
        self.assertNotContains(response, "Private freelancer draft criterion")

    def test_delivery_rows_render_identically_on_sign_and_portal_surfaces(self):
        """Shared partial matches unsigned live scope and signed payload replay."""
        approve_criteria(self.project)
        self.project.refresh_from_db()
        parked_item = AcceptanceItem.objects.create(
            project=self.project,
            text="Shared suspended criterion",
            order=2,
            state=AcceptanceItem.State.SUSPENDED,
            submitted_at=timezone.now(),
            approved_at=timezone.now(),
            is_passed=False,
        )
        AcceptanceStep.objects.create(
            item=parked_item,
            text="Shared unfinished step",
            order=1,
            is_done=False,
        )
        self.project.acceptance_items.filter(
            state=AcceptanceItem.State.APPROVED
        ).update(is_passed=True)
        mark_delivered(self.project)
        self.project.refresh_from_db()
        token = make_client_token(self.project, "sign")

        sign_response = Client().get(
            reverse("surface:client-sign", kwargs={"token": token})
        )
        delivered_portal_response = self.client.get(self.detail_url())
        Project.objects.filter(pk=self.project.pk).update(
            status=Project.Status.ATTESTED
        )
        Attestation.objects.create(
            project=self.project,
            payload={
                "acceptance_items": [
                    {
                        "text": parked_item.text,
                        "state": AcceptanceItem.State.SUSPENDED,
                        "is_passed": False,
                        "evidence_url": "",
                        "steps": [
                            {"text": "Shared unfinished step", "is_done": False}
                        ],
                    }
                ]
            },
            payload_hash="a" * 64,
            client_email=self.project.client_email,
            client_name_typed="Parity Signer",
            signature_meta={},
        )
        portal_response = self.client.get(self.detail_url())
        sign_markup = self.delivery_item_markup(sign_response, parked_item.text)

        self.assertEqual(
            sign_markup,
            self.delivery_item_markup(
                delivered_portal_response,
                parked_item.text,
            ),
        )
        self.assertEqual(
            sign_markup,
            self.delivery_item_markup_from_content(
                self.signed_record_markup(portal_response),
                parked_item.text,
            ),
        )

    def test_signed_record_replays_frozen_payload_not_live_rows(self):
        """Signed checklist comes from immutable payload even if live rows differ."""
        Project.objects.filter(pk=self.project.pk).update(
            status=Project.Status.ATTESTED
        )
        live_item = self.project.acceptance_items.get()
        live_item.text = "Changed live criterion after signing"
        live_item.is_passed = False
        live_item.save(update_fields=["text", "is_passed"])
        frozen_text = "Frozen criterion the client signed"
        frozen_step = "Frozen signed step"
        signed_at = datetime(2026, 3, 14, 9, 26, tzinfo=UTC)
        attestation = Attestation.objects.create(
            project=self.project,
            payload={
                "acceptance_items": [
                    {
                        "text": frozen_text,
                        "state": "approved",
                        "is_passed": True,
                        "evidence_url": "",
                        "steps": [{"text": frozen_step, "is_done": True}],
                    }
                ]
            },
            payload_hash="f" * 64,
            client_email=self.project.client_email,
            client_name_typed="Frozen Signer",
            signed_at=signed_at,
            signature_meta={},
        )

        response = self.client.get(self.detail_url())
        signed_markup = self.signed_record_markup(response)

        self.assert_checklist_mode(response, live=False, signed=True)
        self.assertIn(frozen_text, signed_markup)
        self.assertIn(frozen_step, signed_markup)
        self.assertNotIn(live_item.text, signed_markup)
        self.assertNotIn(live_item.text, self.portal_detail_markup(response))
        self.assertIn("<strong>Passed</strong>", signed_markup)
        self.assertNotIn("<strong>Not passed</strong>", signed_markup)
        self.assertIn(attestation.payload_hash, signed_markup)
        self.assertIn("Mar 14, 2026, 9:26 AM UTC", signed_markup)

    def test_signed_record_explains_hash_fingerprint(self):
        """Signed detail explains the intact copyable hash in client language."""
        Project.objects.filter(pk=self.project.pk).update(
            status=Project.Status.ATTESTED
        )
        payload_hash = "e" * 64
        Attestation.objects.create(
            project=self.project,
            payload={"acceptance_items": []},
            payload_hash=payload_hash,
            client_email=self.project.client_email,
            client_name_typed="Hash Explanation Signer",
            signature_meta={},
        )

        response = self.client.get(self.detail_url())
        signed_markup = self.signed_record_markup(response)

        self.assertIn(
            "This is a fingerprint of the exact record you signed; if any detail "
            "is altered, the fingerprint changes so the record can be checked later.",
            signed_markup,
        )
        self.assertIn(
            f'<span class="break-word record-hash">{payload_hash}</span>',
            signed_markup,
        )

    def test_legacy_signed_payload_missing_item_fields_renders_safely(self):
        """Legacy checklist items without newer keys remain readable."""
        Project.objects.filter(pk=self.project.pk).update(
            status=Project.Status.ATTESTED
        )
        legacy_text = "Legacy frozen criterion"
        Attestation.objects.create(
            project=self.project,
            payload={"acceptance_items": [{"text": legacy_text}]},
            payload_hash="b" * 64,
            client_email=self.project.client_email,
            client_name_typed="Legacy Signer",
            signature_meta={},
        )

        response = self.client.get(self.detail_url())
        signed_markup = self.signed_record_markup(response)

        self.assertEqual(response.status_code, 200)
        self.assertIn(legacy_text, signed_markup)
        self.assertIn("<strong>No result recorded</strong>", signed_markup)
        self.assertNotIn("excluded from this delivery", signed_markup)

    def test_portal_replays_current_amendment_not_superseded_record(self):
        """Portal selects the current amendment at the signed-record call site."""
        approve_criteria(self.project)
        self.project.refresh_from_db()
        item = self.project.acceptance_items.get()
        item.is_passed = True
        item.save(update_fields=["is_passed"])
        mark_delivered(self.project)
        original = sign_attestation(
            self.project,
            self.project.client_email,
            "Original Signer",
            {},
        )
        amended_text = "Criterion frozen in current amendment"
        AcceptanceItem.objects.filter(pk=item.pk).update(text=amended_text)
        amendment = amend_attestation(
            original,
            self.project.client_email,
            "Amendment Signer",
            {},
        )

        response = self.client.get(self.detail_url())
        signed_markup = self.signed_record_markup(response)

        self.assertIn(amended_text, signed_markup)
        self.assertIn(amendment.payload_hash, signed_markup)
        self.assertNotIn(original.payload_hash, signed_markup)
        self.assertNotIn(original.payload["acceptance_items"][0]["text"], signed_markup)

    def test_detail_query_count_is_flat_across_multiple_items(self):
        """Detail prefetches step rows instead of querying once per criterion."""
        AcceptanceItem.objects.create(
            project=self.project,
            text="Second submitted criterion",
            order=2,
            state=AcceptanceItem.State.SUBMITTED,
            submitted_at=timezone.now(),
        )

        with self.assertNumQueries(6):
            response = self.client.get(self.detail_url())

        self.assertEqual(response.status_code, 200)

    def test_unsigned_statuses_show_live_scope_without_signed_record(self):
        """Pending, active, and delivered projects retain the live progress view."""
        for status in (
            Project.Status.CRITERIA_PENDING,
            Project.Status.ACTIVE,
            Project.Status.DELIVERED,
        ):
            with self.subTest(status=status):
                Project.objects.filter(pk=self.project.pk).update(status=status)
                response = self.client.get(self.detail_url())

                self.assert_checklist_mode(response, live=True, signed=False)

    def test_every_visible_status_renders_exactly_one_checklist(self):
        """No client-visible status can render live and signed checklists together."""
        for status in (
            Project.Status.CRITERIA_PENDING,
            Project.Status.ACTIVE,
            Project.Status.DELIVERED,
        ):
            with self.subTest(status=status):
                Project.objects.filter(pk=self.project.pk).update(status=status)
                response = self.client.get(self.detail_url())
                self.assert_checklist_mode(response, live=True, signed=False)

        Attestation.objects.create(
            project=self.project,
            payload={"acceptance_items": []},
            payload_hash="c" * 64,
            client_email=self.project.client_email,
            client_name_typed="Checklist Mode Signer",
            signature_meta={},
        )
        for status in (Project.Status.ATTESTED, Project.Status.DISPUTED):
            with self.subTest(status=status):
                Project.objects.filter(pk=self.project.pk).update(status=status)
                response = self.client.get(self.detail_url())
                self.assert_checklist_mode(response, live=False, signed=True)

    def test_disputed_project_is_explicitly_labelled_not_clean(self):
        """Disputed signed work carries both status and non-clean warning."""
        clean_response = self.client.get(self.detail_url())
        self.assertNotContains(
            clean_response,
            "not presented as a clean attestation",
        )
        self.assertNotContains(clean_response, 'class="dispute-notice"', html=False)
        Project.objects.filter(pk=self.project.pk).update(
            status=Project.Status.DISPUTED
        )
        Attestation.objects.create(
            project=self.project,
            payload={
                "acceptance_items": [
                    {
                        "text": "Disputed frozen criterion",
                        "state": "approved",
                        "is_passed": True,
                        "steps": [],
                    }
                ]
            },
            payload_hash="d" * 64,
            client_email=self.project.client_email,
            client_name_typed="Disputed Signer",
            signature_meta={},
            is_disputed=True,
            disputed_at=timezone.now(),
        )

        response = self.client.get(self.detail_url())

        self.assert_checklist_mode(response, live=False, signed=True)
        self.assertContains(response, "Disputed")
        self.assertContains(response, "not presented as a clean attestation")
        self.assertContains(response, 'class="dispute-notice"', html=False)

    def test_detail_has_no_mutating_controls_and_rejects_post(self):
        """I3b exposes no project action while POST remains unsupported."""
        response = self.client.get(self.detail_url())
        detail_markup = self.portal_detail_markup(response)

        self.assertNotIn("<form", detail_markup)
        self.assertNotIn("<button", detail_markup)
        action_links = re.findall(
            r"<a\b[^>]*>.*?</a>",
            detail_markup,
            flags=re.IGNORECASE | re.DOTALL,
        )
        for link_markup in action_links:
            with self.subTest(link=link_markup):
                self.assertNotRegex(
                    link_markup,
                    r'class="[^"]*\bbutton\b',
                )
                self.assertNotRegex(
                    link_markup,
                    r">\s*[^<]*(?:Confirm|Approve|Sign)\b",
                )
        post_response = self.client.post(self.detail_url())
        self.assertEqual(post_response.status_code, 405)

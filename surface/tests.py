"""Verifier tests for the Surface layer (M2 + M3)."""

from unittest.mock import patch
from urllib.parse import urlparse

from django.contrib.auth import get_user_model
from django.core import mail, signing
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from ledger.models import AcceptanceItem, ChangeOrder, Profile, Project
from ledger.services import (
    approve_criteria,
    flag_dispute,
    mark_delivered,
    sign_attestation,
    submit_criteria_for_approval,
)
from surface import billing
from surface.auth import MAGIC_LOGIN_MAX_AGE, make_magic_login_token
from surface.tokens import (
    CLIENT_TOKEN_MAX_AGE,
    make_change_order_token,
    make_client_token,
    read_client_token,
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
        username=handle,
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
    """Return the path portion of the most recent magic-link email body."""
    body = mail.outbox[-1].body.strip()
    return urlparse(body).path


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


class MagicLinkTests(TestCase):
    """Passwordless freelancer login via signed email links."""

    @override_settings(**LOCMem_EMAIL)
    def test_request_email_then_valid_token_establishes_session(self):
        """Requesting a link and visiting it logs the freelancer in."""
        client = Client()
        email = "freelancer@example.com"

        response = request_magic_link(client, email)
        self.assertRedirects(response, reverse("surface:login-sent"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/login/", mail.outbox[0].body)

        login_path = login_path_from_outbox()
        response = client.get(login_path)
        self.assertRedirects(response, reverse("surface:project-list"))

        session = client.session
        user = get_user_model().objects.get(email=email)
        self.assertEqual(int(session["_auth_user_id"]), user.pk)

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
                reverse("surface:magic-login", kwargs={"token": token})
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
            reverse("surface:magic-login", kwargs={"token": tampered})
        )
        self.assertContains(response, "This login link is no longer available")
        self.assertNotIn("_auth_user_id", client.session)

    def test_malformed_magic_token_shows_link_error_without_session(self):
        """Garbage token paths render link_error and do not authenticate."""
        client = Client()
        response = client.get(
            reverse("surface:magic-login", kwargs={"token": "not-a-valid-token"})
        )
        self.assertContains(response, "This login link is no longer available")
        self.assertNotIn("_auth_user_id", client.session)


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

    def test_criterion_mutations_return_404_when_locked(self):
        """Create, update, and delete return 404 after criteria are locked."""
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
            self.client.post(create_url, {"text": "Blocked", "order": 2}).status_code,
            404,
        )
        self.assertFalse(
            self.project.acceptance_items.filter(text="Blocked").exists()
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

    def test_client_approve_activates_project(self):
        """Valid approve POST activates the project through ledger.services."""
        response = self.client.post(
            reverse("surface:client-approve", kwargs={"token": self.token})
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

    def test_client_approve_already_handled_shows_thanks_and_info(self):
        """A second approve shows an informational message and the thanks page."""
        approve_criteria(self.project)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.ACTIVE)

        response = self.client.post(
            reverse("surface:client-approve", kwargs={"token": self.token})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Thank you")
        self.assertContains(response, "These criteria were already handled.")


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

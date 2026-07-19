"""Verifier tests for the Surface layer (M2)."""

from unittest.mock import patch
from urllib.parse import urlparse

from django.contrib.auth import get_user_model
from django.core import mail, signing
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from ledger.models import AcceptanceItem, Profile, Project
from ledger.services import approve_criteria, submit_criteria_for_approval
from surface.auth import MAGIC_LOGIN_MAX_AGE, make_magic_login_token
from surface.tokens import CLIENT_TOKEN_MAX_AGE, make_client_token, read_client_token


_profile_counter = 0

LOCMem_EMAIL = {"EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend"}


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


def login_as(client, profile):
    """Establish an authenticated session as the given profile's user."""
    client.force_login(profile.user)


def request_magic_link(client, email):
    """POST a magic-link request and return the response."""
    return client.post(reverse("surface:login-request"), {"email": email})


def login_path_from_outbox():
    """Return the path portion of the most recent magic-link email body."""
    body = mail.outbox[-1].body.strip()
    return urlparse(body).path


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

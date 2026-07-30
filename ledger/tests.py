"""Verifier tests for the Ledger core (M1)."""

import json
from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.signals import post_delete, post_save, pre_delete
from django.test import Client, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from ledger.models import (
    AcceptanceItem,
    AcceptanceStep,
    Attestation,
    CapabilityTag,
    ChangeOrder,
    ImmutableAttestation,
    Profile,
    Project,
)
from ledger.services import (
    InvalidSignatureMeta,
    InvalidTransition,
    acceptance_item_locked,
    acceptance_step_locked,
    amend_attestation,
    approve_acceptance_item,
    approve_acceptance_items,
    approve_change_order,
    approve_criteria,
    canonical_payload,
    compute_payload_hash,
    create_acceptance_step,
    criteria_locked,
    decline_change_order,
    delete_acceptance_item,
    delete_acceptance_step,
    disputed_count,
    flag_dispute,
    has_open_change_orders,
    mark_delivered,
    propose_change_order,
    public_attestations,
    pull_back_acceptance_item,
    recompute_capability_tags,
    recompute_capability_tags_after_attestation_save,
    reopen_active,
    resolve_dispute,
    resume_acceptance_item,
    set_acceptance_step_done,
    set_profile_details,
    set_profile_visibility,
    sign_attestation,
    submit_acceptance_item_for_approval,
    submit_acceptance_items_for_approval,
    submit_criteria_for_approval,
    suspend_acceptance_item,
    update_acceptance_step,
    verify_payload_hash,
    withdraw_acceptance_item,
)


def safe_signature_meta():
    """Return signature metadata that passes service validation."""
    return {"user_agent": "TestAgent/1.0", "ip_hash": "abc123def456"}


DRAFT_SUBMITTED_BLOCK_DELIVERY = (
    "Draft or submitted acceptance items block delivery."
)
VACUOUS_DELIVERY = (
    "At least one approved acceptance item is required before delivery."
)
DRAFT_SUBMITTED_BLOCK_SIGNING = (
    "Draft or submitted acceptance items block signing."
)
VACUOUS_SIGNING = (
    "At least one approved acceptance item is required before signing."
)
DELIVERY_FAILED_ITEM = "Every delivery item must pass before delivery."
SIGNING_FAILED_ITEM = "Every acceptance item must pass before signing."
STALE_ACCEPTANCE_ITEM_TRANSITION = (
    "Acceptance item state changed before transition could be saved."
)
DELETED_ACCEPTANCE_ITEM_TRANSITION = "Acceptance item no longer exists."
APPROVED_ITEM_DELETE_REFUSAL = (
    "Client-approved acceptance items cannot be deleted."
)
FROZEN_SCOPE_DELETE_REFUSAL = (
    "Acceptance items cannot be deleted after project delivery."
)


_profile_counter = 0


def make_profile(handle=None):
    """Create a user and profile for test fixtures."""
    global _profile_counter
    _profile_counter += 1
    if handle is None:
        handle = f"freelancer-{_profile_counter}"
    user = User.objects.create_user(username=handle, password="testpass")
    return Profile.objects.create(
        user=user,
        handle=handle,
        display_name="Test Freelancer",
    )


def make_project_with_items(status=Project.Status.DRAFT, skills_csv="", owner=None):
    """Create a project with two acceptance criteria at the given status."""
    if owner is None:
        owner = make_profile()
    project = Project.objects.create(
        owner=owner,
        title="Test Project",
        client_name="Acme Corp",
        client_email="client@acme.com",
        brief="Build a thing",
        skills_csv=skills_csv,
        status=status,
    )
    item_state = AcceptanceItem.State.DRAFT
    submitted_at = None
    approved_at = None
    state_timestamp = timezone.now()
    if status == Project.Status.CRITERIA_PENDING:
        item_state = AcceptanceItem.State.SUBMITTED
        submitted_at = state_timestamp
    elif status in {
        Project.Status.ACTIVE,
        Project.Status.DELIVERED,
        Project.Status.ATTESTED,
        Project.Status.DISPUTED,
    }:
        item_state = AcceptanceItem.State.APPROVED
        submitted_at = state_timestamp
        approved_at = state_timestamp
    item_fields = {
        "project": project,
        "state": item_state,
        "submitted_at": submitted_at,
        "approved_at": approved_at,
        "is_passed": None,
    }
    AcceptanceItem.objects.create(
        text="Criterion one",
        order=1,
        **item_fields,
    )
    AcceptanceItem.objects.create(
        text="Criterion two",
        order=2,
        **item_fields,
    )
    return project


def advance_to_delivered(project):
    """Walk a project through legal transitions until it is delivered."""
    project.refresh_from_db()
    if project.status == Project.Status.DRAFT:
        submit_criteria_for_approval(project)
    if project.status == Project.Status.CRITERIA_PENDING:
        approve_criteria(project)
    if project.status == Project.Status.ACTIVE:
        mark_delivered(project)
    project.refresh_from_db()
    return project


def mark_all_items_passed(project):
    """Mark every acceptance item approved and passed."""
    state_timestamp = timezone.now()
    project.acceptance_items.update(
        state=AcceptanceItem.State.APPROVED,
        submitted_at=state_timestamp,
        approved_at=state_timestamp,
        is_passed=True,
    )


def sign_project(project, client_email="signer@acme.com", client_name="Signer Name"):
    """Deliver (if needed), verify items, and sign the project."""
    if project.status != Project.Status.DELIVERED:
        advance_to_delivered(project)
    mark_all_items_passed(project)
    return sign_attestation(
        project,
        client_email,
        client_name,
        safe_signature_meta(),
    )


class ProfileVisibilityTests(TestCase):
    """Profile visibility defaults, service writes, and derivation isolation."""

    def test_directly_created_profile_defaults_to_private(self):
        """A Profile created without visibility is private by default."""
        profile = make_profile()

        self.assertIs(profile.is_public, False)
        profile.refresh_from_db()
        self.assertIs(profile.is_public, False)

    def test_set_profile_visibility_persists_both_directions(self):
        """The service publishes and unpublishes the same Profile."""
        profile = make_profile()

        returned = set_profile_visibility(profile, True)
        profile.refresh_from_db()
        self.assertEqual(returned.pk, profile.pk)
        self.assertIs(profile.is_public, True)

        set_profile_visibility(profile, False)
        profile.refresh_from_db()
        self.assertIs(profile.is_public, False)

    def test_set_profile_visibility_rejects_invalid_inputs(self):
        """The service requires a boolean visibility value."""
        profile = make_profile()

        with self.assertRaises(ValueError):
            set_profile_visibility(profile, "public")

    def test_visibility_does_not_change_derivation_or_public_attestations(self):
        """Visibility never changes signed evidence or capability derivation."""
        profile = make_profile()
        project = make_project_with_items(
            status=Project.Status.DELIVERED,
            skills_csv="django, htmx",
            owner=profile,
        )
        attestation = sign_project(project)
        private_tags = list(
            CapabilityTag.objects.filter(profile=profile)
            .order_by("name")
            .values_list("name", "attested_count", "last_attested_at")
        )
        private_attestation_ids = [
            row.pk for row in public_attestations(profile)
        ]

        set_profile_visibility(profile, True)
        recompute_capability_tags(profile)

        public_tags = list(
            CapabilityTag.objects.filter(profile=profile)
            .order_by("name")
            .values_list("name", "attested_count", "last_attested_at")
        )
        public_attestation_ids = [
            row.pk for row in public_attestations(profile)
        ]
        self.assertEqual(public_tags, private_tags)
        self.assertEqual(
            public_attestation_ids,
            private_attestation_ids,
        )
        self.assertEqual(public_attestation_ids, [attestation.pk])


class SetProfileDetailsTests(TestCase):
    """Profile identity field updates via set_profile_details."""

    def test_set_profile_details_partial_update_preserves_other_fields(self):
        """Updating one field must not erase the other four in the database."""
        profile = make_profile()
        set_profile_details(
            profile,
            display_name="Full Name",
            headline="Headline",
            bio="Bio text",
            location="City",
            website_url="https://example.com",
        )

        set_profile_details(profile, bio="Updated bio only")

        stored = Profile.objects.get(pk=profile.pk)
        self.assertEqual(stored.display_name, "Full Name")
        self.assertEqual(stored.headline, "Headline")
        self.assertEqual(stored.bio, "Updated bio only")
        self.assertEqual(stored.location, "City")
        self.assertEqual(stored.website_url, "https://example.com")

    def test_set_profile_details_limits_update_fields_to_supplied_keys(self):
        """save() must receive update_fields covering only supplied columns."""
        profile = make_profile()
        set_profile_details(
            profile,
            display_name="Name",
            headline="Head",
            bio="Bio",
            location="Here",
            website_url="https://example.com",
        )
        profile = Profile.objects.get(pk=profile.pk)
        profile.headline = "Mutated in memory"

        with patch.object(profile, "save", wraps=profile.save) as mock_save:
            set_profile_details(profile, location="New City")
            mock_save.assert_called_once()
            _, kwargs = mock_save.call_args
            self.assertEqual(kwargs.get("update_fields"), ("location",))

        stored = Profile.objects.get(pk=profile.pk)
        self.assertEqual(stored.headline, "Head")
        self.assertEqual(stored.location, "New City")

    def test_set_profile_details_rejects_handle_change(self):
        """Handle immutability is enforced before any write."""
        profile = make_profile(handle="immutable-handle")
        original_handle = profile.handle

        with self.assertRaisesMessage(ValueError, "handle cannot be changed."):
            set_profile_details(profile, handle="new-handle")

        stored = Profile.objects.get(pk=profile.pk)
        self.assertEqual(stored.handle, original_handle)

    def test_set_profile_details_rejects_unknown_keyword(self):
        """Unsupported keys raise before persisting anything."""
        profile = make_profile()
        set_profile_details(profile, display_name="Name", bio="Bio")

        with self.assertRaisesMessage(
            ValueError,
            "Profile details contain unsupported fields.",
        ):
            set_profile_details(profile, is_public=True)

        stored = Profile.objects.get(pk=profile.pk)
        self.assertEqual(stored.display_name, "Name")
        self.assertEqual(stored.bio, "Bio")
        self.assertIs(stored.is_public, False)

    def test_set_profile_details_rejects_empty_call(self):
        """At least one supported field must be supplied."""
        profile = make_profile()
        set_profile_details(profile, display_name="Name")

        with self.assertRaisesMessage(
            ValueError,
            "At least one profile field must be supplied.",
        ):
            set_profile_details(profile)

        stored = Profile.objects.get(pk=profile.pk)
        self.assertEqual(stored.display_name, "Name")

    def test_set_profile_details_rejects_blank_display_name(self):
        """display_name must remain a non-empty string."""
        profile = make_profile()
        set_profile_details(profile, display_name="Valid Name")

        for bad_value in ("", "   ", 123):
            with self.subTest(bad_value=bad_value):
                with self.assertRaisesMessage(
                    ValueError,
                    "display_name must not be empty.",
                ):
                    set_profile_details(profile, display_name=bad_value)
                stored = Profile.objects.get(pk=profile.pk)
                self.assertEqual(stored.display_name, "Valid Name")

    def test_set_profile_details_strips_display_name_whitespace(self):
        """Surrounding whitespace is removed from display_name before save."""
        profile = make_profile()
        set_profile_details(profile, display_name="  Trimmed Name  ")

        stored = Profile.objects.get(pk=profile.pk)
        self.assertEqual(stored.display_name, "Trimmed Name")

    def test_set_profile_details_rejects_malformed_website_url(self):
        """Invalid website_url values fail validation without persisting."""
        profile = make_profile()
        set_profile_details(
            profile,
            display_name="Name",
            website_url="https://example.com",
        )

        with self.assertRaises(ValidationError):
            set_profile_details(profile, website_url="not-a-url")

        stored = Profile.objects.get(pk=profile.pk)
        self.assertEqual(stored.website_url, "https://example.com")

    def test_set_profile_details_rejects_overlong_location(self):
        """location longer than 120 characters is rejected."""
        profile = make_profile()
        set_profile_details(
            profile,
            display_name="Name",
            location="Short",
        )

        with self.assertRaises(ValidationError):
            set_profile_details(profile, location="x" * 121)

        stored = Profile.objects.get(pk=profile.pk)
        self.assertEqual(stored.location, "Short")

    def test_set_profile_details_persists_valid_full_update(self):
        """All five identity fields persist when supplied together."""
        profile = make_profile()
        set_profile_details(
            profile,
            display_name="Full Name",
            headline="Headline",
            bio="Bio text",
            location="City",
            website_url="https://example.com/path",
        )

        stored = Profile.objects.get(pk=profile.pk)
        self.assertEqual(stored.display_name, "Full Name")
        self.assertEqual(stored.headline, "Headline")
        self.assertEqual(stored.bio, "Bio text")
        self.assertEqual(stored.location, "City")
        self.assertEqual(stored.website_url, "https://example.com/path")

    def test_set_profile_details_can_clear_optional_field_with_empty_string(self):
        """Explicit empty string clears an optional field; absence leaves it."""
        profile = make_profile()
        set_profile_details(
            profile,
            display_name="Name",
            bio="Has bio",
            headline="Headline",
        )

        set_profile_details(profile, bio="")
        stored = Profile.objects.get(pk=profile.pk)
        self.assertEqual(stored.bio, "")
        self.assertEqual(stored.headline, "Headline")

        set_profile_details(profile, headline="New headline")
        stored = Profile.objects.get(pk=profile.pk)
        self.assertEqual(stored.bio, "")
        self.assertEqual(stored.headline, "New headline")


class ProfileIsPublicMigrationTests(TransactionTestCase):
    """Migration 0002 adds reversible is_public with a private default."""

    def test_0002_profile_is_public_applies_and_reverses(self):
        """The is_public field can be applied forward and rolled back cleanly."""
        executor = MigrationExecutor(connection)
        app_label = "ledger"
        initial_migration = "0001_initial"
        visibility_migration = "0002_profile_is_public"
        latest_migration = next(
            name
            for app, name in executor.loader.graph.leaf_nodes()
            if app == app_label
        )

        def profile_columns():
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA table_info(ledger_profile)")
                return {row[1] for row in cursor.fetchall()}

        executor.migrate([(app_label, initial_migration)])
        initial_state = executor.loader.project_state((app_label, initial_migration))
        InitialProfile = initial_state.apps.get_model(app_label, "Profile")
        self.assertNotIn(
            "is_public",
            {field.name for field in InitialProfile._meta.get_fields()},
        )
        self.assertNotIn("is_public", profile_columns())

        executor = MigrationExecutor(connection)
        executor.migrate([(app_label, visibility_migration)])
        applied_state = executor.loader.project_state((app_label, visibility_migration))
        AppliedProfile = applied_state.apps.get_model(app_label, "Profile")
        is_public_field = AppliedProfile._meta.get_field("is_public")
        self.assertIs(is_public_field.default, False)
        self.assertIn("is_public", profile_columns())

        user = User.objects.create_user(username="migration-visibility")
        profile = AppliedProfile.objects.create(
            user_id=user.pk,
            handle="migration-visibility",
            display_name="Migration Visibility",
        )
        profile.refresh_from_db()
        self.assertIs(profile.is_public, False)

        executor = MigrationExecutor(connection)
        executor.migrate([(app_label, initial_migration)])
        reversed_state = executor.loader.project_state((app_label, initial_migration))
        ReversedProfile = reversed_state.apps.get_model(app_label, "Profile")
        self.assertNotIn(
            "is_public",
            {field.name for field in ReversedProfile._meta.get_fields()},
        )
        self.assertNotIn("is_public", profile_columns())

        executor = MigrationExecutor(connection)
        executor.migrate([(app_label, latest_migration)])


class AcceptanceItemStateMigrationTests(TransactionTestCase):
    """Migration 0003 truthfully derives existing item approval state."""

    def test_0003_backfills_each_project_status_and_reverses(self):
        """Every legacy project status maps to the specified item state."""
        executor = MigrationExecutor(connection)
        app_label = "ledger"
        before_migration = "0002_profile_is_public"
        item_state_migration = (
            "0003_acceptanceitem_approved_at_acceptanceitem_state_and_more"
        )
        latest_migration = next(
            name
            for app, name in executor.loader.graph.leaf_nodes()
            if app == app_label
        )
        executor.migrate([(app_label, before_migration)])
        before_state = executor.loader.project_state(
            (app_label, before_migration)
        )
        HistoricalProfile = before_state.apps.get_model(app_label, "Profile")
        HistoricalProject = before_state.apps.get_model(app_label, "Project")
        HistoricalItem = before_state.apps.get_model(
            app_label,
            "AcceptanceItem",
        )
        user = User.objects.create_user(username="migration-owner")
        profile = HistoricalProfile.objects.create(
            user_id=user.pk,
            handle="migration-owner",
            display_name="Migration Owner",
        )
        statuses = (
            "draft",
            "criteria_pending",
            "active",
            "delivered",
            "attested",
            "disputed",
        )
        project_updates = {}
        for order, status in enumerate(statuses, start=1):
            project = HistoricalProject.objects.create(
                owner_id=profile.pk,
                title=f"Migration {status}",
                client_name="Client",
                client_email=f"{status}@example.com",
                brief="Legacy project",
                status=status,
            )
            project_updates[status] = project.updated_at
            HistoricalItem.objects.create(
                project_id=project.pk,
                text=status,
                order=order,
            )
        HistoricalProject.objects.create(
            owner_id=profile.pk,
            title="Migration zero items",
            client_name="Client",
            client_email="zero-items@example.com",
            brief="Legacy project without criteria",
            status="active",
        )

        executor = MigrationExecutor(connection)
        executor.migrate([(app_label, item_state_migration)])
        applied_state = executor.loader.project_state(
            (app_label, item_state_migration)
        )
        AppliedItem = applied_state.apps.get_model(
            app_label,
            "AcceptanceItem",
        )
        AppliedProject = applied_state.apps.get_model(
            app_label,
            "Project",
        )
        items = {
            item.text: item
            for item in AppliedItem.objects.order_by("text")
        }
        self.assertEqual(items["draft"].state, "draft")
        self.assertIsNone(items["draft"].submitted_at)
        self.assertIsNone(items["draft"].approved_at)
        self.assertEqual(items["criteria_pending"].state, "submitted")
        self.assertEqual(
            items["criteria_pending"].submitted_at,
            project_updates["criteria_pending"],
        )
        self.assertIsNone(items["criteria_pending"].approved_at)
        for status in ("active", "delivered", "attested", "disputed"):
            self.assertEqual(items[status].state, "approved")
            self.assertEqual(
                items[status].submitted_at,
                project_updates[status],
            )
            self.assertEqual(
                items[status].approved_at,
                project_updates[status],
            )
        self.assertEqual(
            AppliedProject.objects.filter(title="Migration zero items").count(),
            1,
        )
        self.assertEqual(
            AppliedItem.objects.filter(
                project__title="Migration zero items",
            ).count(),
            0,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([(app_label, before_migration)])
        reversed_state = executor.loader.project_state(
            (app_label, before_migration)
        )
        ReversedItem = reversed_state.apps.get_model(
            app_label,
            "AcceptanceItem",
        )
        reversed_fields = {
            field.name for field in ReversedItem._meta.get_fields()
        }
        self.assertNotIn("state", reversed_fields)
        self.assertNotIn("submitted_at", reversed_fields)
        self.assertNotIn("approved_at", reversed_fields)

        executor = MigrationExecutor(connection)
        executor.migrate([(app_label, latest_migration)])


class AcceptanceStepMigrationTests(TransactionTestCase):
    """Migration 0004 creates and cleanly removes the step table."""

    def test_0004_acceptance_step_applies_and_reverses(self):
        """The AcceptanceStep model can be applied and rolled back cleanly."""
        executor = MigrationExecutor(connection)
        app_label = "ledger"
        before_migration = (
            "0003_acceptanceitem_approved_at_acceptanceitem_state_and_more"
        )
        step_migration = "0004_acceptancestep"
        latest_migration = next(
            name
            for app, name in executor.loader.graph.leaf_nodes()
            if app == app_label
        )

        executor.migrate([(app_label, before_migration)])
        before_state = executor.loader.project_state(
            (app_label, before_migration)
        )
        before_models = {
            model._meta.model_name for model in before_state.apps.get_models()
        }
        self.assertNotIn("acceptancestep", before_models)
        self.assertNotIn(
            "ledger_acceptancestep",
            connection.introspection.table_names(),
        )

        executor = MigrationExecutor(connection)
        executor.migrate([(app_label, step_migration)])
        applied_state = executor.loader.project_state(
            (app_label, step_migration)
        )
        AppliedStep = applied_state.apps.get_model(app_label, "AcceptanceStep")
        self.assertEqual(
            {field.name for field in AppliedStep._meta.get_fields()},
            {"id", "item", "text", "order", "is_done", "created_at"},
        )
        self.assertIn(
            "ledger_acceptancestep",
            connection.introspection.table_names(),
        )

        executor = MigrationExecutor(connection)
        executor.migrate([(app_label, before_migration)])
        reversed_state = executor.loader.project_state(
            (app_label, before_migration)
        )
        reversed_models = {
            model._meta.model_name for model in reversed_state.apps.get_models()
        }
        self.assertNotIn("acceptancestep", reversed_models)
        self.assertNotIn(
            "ledger_acceptancestep",
            connection.introspection.table_names(),
        )

        executor = MigrationExecutor(connection)
        executor.migrate([(app_label, latest_migration)])


class ProfileDetailFieldsMigrationTests(TransactionTestCase):
    """Migration 0005 adds reversible profile identity fields."""

    def test_0005_profile_detail_fields_applies_and_reverses(self):
        """bio, location, and website_url apply forward and roll back cleanly."""
        executor = MigrationExecutor(connection)
        app_label = "ledger"
        before_migration = "0004_acceptancestep"
        detail_fields_migration = (
            "0005_profile_bio_profile_location_profile_website_url"
        )
        latest_migration = next(
            name
            for app, name in executor.loader.graph.leaf_nodes()
            if app == app_label
        )
        new_columns = {"bio", "location", "website_url"}

        def profile_columns():
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA table_info(ledger_profile)")
                return {row[1] for row in cursor.fetchall()}

        executor.migrate([(app_label, before_migration)])
        before_state = executor.loader.project_state((app_label, before_migration))
        BeforeProfile = before_state.apps.get_model(app_label, "Profile")
        before_field_names = {
            field.name for field in BeforeProfile._meta.get_fields()
        }
        self.assertFalse(new_columns & before_field_names)
        self.assertFalse(new_columns & profile_columns())

        executor = MigrationExecutor(connection)
        executor.migrate([(app_label, detail_fields_migration)])
        applied_state = executor.loader.project_state(
            (app_label, detail_fields_migration)
        )
        AppliedProfile = applied_state.apps.get_model(app_label, "Profile")
        applied_field_names = {
            field.name for field in AppliedProfile._meta.get_fields()
        }
        self.assertTrue(new_columns <= applied_field_names)
        self.assertTrue(new_columns <= profile_columns())
        bio_field = AppliedProfile._meta.get_field("bio")
        location_field = AppliedProfile._meta.get_field("location")
        website_url_field = AppliedProfile._meta.get_field("website_url")
        self.assertIsInstance(bio_field, models.TextField)
        self.assertIsInstance(location_field, models.CharField)
        self.assertEqual(location_field.max_length, 120)
        self.assertIsInstance(website_url_field, models.URLField)

        executor = MigrationExecutor(connection)
        executor.migrate([(app_label, before_migration)])
        reversed_state = executor.loader.project_state((app_label, before_migration))
        ReversedProfile = reversed_state.apps.get_model(app_label, "Profile")
        reversed_field_names = {
            field.name for field in ReversedProfile._meta.get_fields()
        }
        self.assertFalse(new_columns & reversed_field_names)
        self.assertFalse(new_columns & profile_columns())

        executor = MigrationExecutor(connection)
        executor.migrate([(app_label, latest_migration)])


class StatusMachineTests(TestCase):
    """Legal and illegal project status transitions via services."""

    def test_full_happy_path_transitions_succeed(self):
        """draft→criteria_pending→active→delivered→attested→disputed→attested."""
        project = make_project_with_items()
        self.assertEqual(project.status, Project.Status.DRAFT)

        submit_criteria_for_approval(project)
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.CRITERIA_PENDING)

        approve_criteria(project)
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.ACTIVE)

        mark_delivered(project)
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.DELIVERED)

        attestation = sign_project(project)
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.ATTESTED)
        self.assertTrue(attestation.is_current)

        flag_dispute(project)
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.DISPUTED)

        resolve_dispute(project)
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.ATTESTED)

    def test_draft_to_delivered_raises_invalid_transition(self):
        """Cannot skip workflow states by marking delivered from draft."""
        project = make_project_with_items(status=Project.Status.DRAFT)
        with self.assertRaises(InvalidTransition):
            mark_delivered(project)

    def test_active_to_attested_directly_raises_invalid_transition(self):
        """Signing requires delivered status, not active."""
        project = make_project_with_items(status=Project.Status.ACTIVE)
        mark_all_items_passed(project)
        with self.assertRaises(InvalidTransition):
            sign_attestation(
                project,
                "signer@acme.com",
                "Signer",
                safe_signature_meta(),
            )

    def test_sign_on_non_delivered_raises_invalid_transition(self):
        """Each pre-delivered status rejects attestation signing."""
        for status in (
            Project.Status.DRAFT,
            Project.Status.CRITERIA_PENDING,
            Project.Status.ACTIVE,
        ):
            project = make_project_with_items(status=status)
            mark_all_items_passed(project)
            with self.assertRaises(InvalidTransition):
                sign_attestation(
                    project,
                    "signer@acme.com",
                    "Signer",
                    safe_signature_meta(),
                )

    def test_criteria_locked_false_in_draft_and_criteria_pending(self):
        """Criteria remain editable until the project is active."""
        draft = make_project_with_items(status=Project.Status.DRAFT)
        pending = make_project_with_items(status=Project.Status.CRITERIA_PENDING)
        self.assertFalse(criteria_locked(draft))
        self.assertFalse(criteria_locked(pending))

    def test_criteria_locked_true_after_active(self):
        """Once active (or later), criteria are locked."""
        for status in (
            Project.Status.ACTIVE,
            Project.Status.DELIVERED,
            Project.Status.ATTESTED,
            Project.Status.DISPUTED,
        ):
            project = make_project_with_items(status=status)
            self.assertTrue(criteria_locked(project))

    def test_flag_dispute_on_non_attested_project_raises(self):
        """Only attested projects can be disputed."""
        for status in (
            Project.Status.DRAFT,
            Project.Status.ACTIVE,
            Project.Status.DELIVERED,
            Project.Status.DISPUTED,
        ):
            project = make_project_with_items(status=status)
            with self.assertRaises(InvalidTransition):
                flag_dispute(project)

    def test_resolve_dispute_on_non_disputed_project_raises(self):
        """Only disputed projects can be resolved."""
        for status in (
            Project.Status.DRAFT,
            Project.Status.ACTIVE,
            Project.Status.DELIVERED,
            Project.Status.ATTESTED,
        ):
            project = make_project_with_items(status=status)
            with self.assertRaises(InvalidTransition):
                resolve_dispute(project)

    def test_reopen_active_is_legal_from_delivered(self):
        """delivered→active is permitted for rework before attestation."""
        project = make_project_with_items(status=Project.Status.ACTIVE)
        mark_delivered(project)
        reopen_active(project)
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.ACTIVE)

    def test_mark_delivered_rejects_failed_acceptance_item(self):
        """An explicitly failed item keeps an active project out of delivery."""
        project = make_project_with_items(status=Project.Status.ACTIVE)
        items = list(project.acceptance_items.order_by("order"))
        items[0].is_passed = True
        items[0].save(update_fields=("is_passed",))
        items[1].is_passed = False
        items[1].save(update_fields=("is_passed",))

        with self.assertRaises(InvalidTransition):
            mark_delivered(project)

        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.ACTIVE)


class AcceptanceItemStateMachineTests(TestCase):
    """Exhaustive per-item lifecycle and compatibility-bridge coverage."""

    def setUp(self):
        """Create one draft item for each state-machine test."""
        self.project = make_project_with_items()
        self.item = self.project.acceptance_items.order_by("order").first()

    def set_state(self, state, *, submitted=False, approved=False):
        """Put the fixture in a deliberate state without exercising services."""
        fixed_time = datetime(
            2026,
            1,
            2,
            3,
            4,
            5,
            tzinfo=datetime_timezone.utc,
        )
        self.item.state = state
        self.item.submitted_at = fixed_time if submitted else None
        self.item.approved_at = fixed_time if approved else None
        self.item.save(
            update_fields=("state", "submitted_at", "approved_at"),
        )
        return fixed_time

    def test_draft_submitted_pullback_and_resubmit_edges(self):
        """draft→submitted→draft→submitted is legal and timestamped."""
        submit_acceptance_item_for_approval(self.item)
        first_submission = self.item.submitted_at
        self.assertEqual(self.item.state, AcceptanceItem.State.SUBMITTED)
        self.assertIsNotNone(first_submission)

        pull_back_acceptance_item(self.item)
        self.assertEqual(self.item.state, AcceptanceItem.State.DRAFT)
        self.assertIsNone(self.item.submitted_at)
        self.assertTrue(acceptance_item_locked(self.item) is False)

        submit_acceptance_item_for_approval(self.item)
        self.assertEqual(self.item.state, AcceptanceItem.State.SUBMITTED)
        self.assertIsNotNone(self.item.submitted_at)

    def test_submitted_path_suspend_resume_round_trip(self):
        """draft→submitted→draft→submitted→suspended→resume stays submitted."""
        submit_acceptance_item_for_approval(self.item)
        pull_back_acceptance_item(self.item)
        submit_acceptance_item_for_approval(self.item)
        submitted_at = self.item.submitted_at
        self.assertIsNone(self.item.approved_at)

        suspend_acceptance_item(self.item)
        self.assertEqual(self.item.state, AcceptanceItem.State.SUSPENDED)

        resume_acceptance_item(self.item)
        self.assertEqual(self.item.state, AcceptanceItem.State.SUBMITTED)
        self.assertIsNone(self.item.approved_at)
        self.assertEqual(self.item.submitted_at, submitted_at)

    def test_submitted_to_approved_edge(self):
        """submitted→approved retains submission time and records approval."""
        submitted_at = self.set_state(
            AcceptanceItem.State.SUBMITTED,
            submitted=True,
        )
        approve_acceptance_item(self.item)
        self.assertEqual(self.item.state, AcceptanceItem.State.APPROVED)
        self.assertEqual(self.item.submitted_at, submitted_at)
        self.assertIsNotNone(self.item.approved_at)

    def test_stale_pullback_cannot_overwrite_client_approval(self):
        """A delayed pull-back cannot erase a competing client approval."""
        self.set_state(
            AcceptanceItem.State.SUBMITTED,
            submitted=True,
        )
        stale_item = AcceptanceItem.objects.get(pk=self.item.pk)
        competing_item = AcceptanceItem.objects.get(pk=self.item.pk)
        approve_acceptance_item(competing_item)

        with self.assertRaises(InvalidTransition) as raised:
            pull_back_acceptance_item(stale_item)

        self.assertEqual(str(raised.exception), STALE_ACCEPTANCE_ITEM_TRANSITION)
        self.assertEqual(stale_item.state, AcceptanceItem.State.APPROVED)
        self.assertEqual(stale_item.approved_at, competing_item.approved_at)
        persisted_item = AcceptanceItem.objects.get(pk=self.item.pk)
        self.assertEqual(persisted_item.state, AcceptanceItem.State.APPROVED)
        self.assertIsNotNone(persisted_item.approved_at)
        self.assertFalse(
            AcceptanceItem.objects.filter(
                pk=self.item.pk,
                state=AcceptanceItem.State.DRAFT,
                approved_at__isnull=False,
            ).exists()
        )

    def test_stale_delete_cannot_destroy_competing_client_approval(self):
        """A delayed delete cannot remove an item approved in the meantime."""
        self.set_state(
            AcceptanceItem.State.SUBMITTED,
            submitted=True,
        )
        stale_item = AcceptanceItem.objects.get(pk=self.item.pk)
        competing_item = AcceptanceItem.objects.get(pk=self.item.pk)
        approve_acceptance_item(competing_item)

        with self.assertRaises(InvalidTransition):
            delete_acceptance_item(stale_item)

        persisted_item = AcceptanceItem.objects.get(pk=self.item.pk)
        self.assertEqual(persisted_item.state, AcceptanceItem.State.APPROVED)
        self.assertIsNotNone(persisted_item.approved_at)

    def test_stale_resume_cannot_downgrade_new_client_approval(self):
        """Resume guards the approval timestamp used to choose its target."""
        submit_acceptance_item_for_approval(self.item)
        suspend_acceptance_item(self.item)
        stale_item = AcceptanceItem.objects.get(pk=self.item.pk)
        competing_item = AcceptanceItem.objects.get(pk=self.item.pk)
        resume_acceptance_item(competing_item)
        approve_acceptance_item(competing_item)
        suspend_acceptance_item(competing_item)

        with self.assertRaises(InvalidTransition) as raised:
            resume_acceptance_item(stale_item)

        self.assertEqual(str(raised.exception), STALE_ACCEPTANCE_ITEM_TRANSITION)
        self.assertEqual(stale_item.state, AcceptanceItem.State.SUSPENDED)
        self.assertEqual(stale_item.approved_at, competing_item.approved_at)
        self.assertFalse(
            AcceptanceItem.objects.filter(
                pk=self.item.pk,
                state=AcceptanceItem.State.SUBMITTED,
                approved_at__isnull=False,
            ).exists()
        )
        self.assertFalse(
            AcceptanceItem.objects.filter(
                pk=self.item.pk,
                state=AcceptanceItem.State.DRAFT,
                approved_at__isnull=False,
            ).exists()
        )

    def test_stale_suspend_rejects_other_legal_source_state(self):
        """Suspend fails closed when submitted moved to approved."""
        submit_acceptance_item_for_approval(self.item)
        stale_item = AcceptanceItem.objects.get(pk=self.item.pk)
        competing_item = AcceptanceItem.objects.get(pk=self.item.pk)
        approve_acceptance_item(competing_item)

        with self.assertRaises(InvalidTransition) as raised:
            suspend_acceptance_item(stale_item)

        self.assertEqual(str(raised.exception), STALE_ACCEPTANCE_ITEM_TRANSITION)
        self.assertEqual(stale_item.state, AcceptanceItem.State.APPROVED)
        self.assertEqual(stale_item.approved_at, competing_item.approved_at)
        persisted_item = AcceptanceItem.objects.get(pk=self.item.pk)
        self.assertEqual(persisted_item.state, AcceptanceItem.State.APPROVED)
        self.assertFalse(
            AcceptanceItem.objects.filter(
                pk=self.item.pk,
                state__in=(
                    AcceptanceItem.State.DRAFT,
                    AcceptanceItem.State.SUBMITTED,
                ),
                approved_at__isnull=False,
            ).exists()
        )

    def test_transition_reports_concurrently_deleted_item(self):
        """A deleted row has a distinct fail-closed transition error."""
        submit_acceptance_item_for_approval(self.item)
        stale_item = AcceptanceItem.objects.get(pk=self.item.pk)
        AcceptanceItem.objects.filter(pk=self.item.pk).delete()

        with self.assertRaises(InvalidTransition) as raised:
            pull_back_acceptance_item(stale_item)

        self.assertEqual(str(raised.exception), DELETED_ACCEPTANCE_ITEM_TRANSITION)
        self.assertFalse(
            AcceptanceItem.objects.filter(pk=self.item.pk).exists()
        )

    def test_submitted_suspend_and_resume_edge(self):
        """submitted→suspended→submitted retains historical timestamps."""
        submitted_at = self.set_state(
            AcceptanceItem.State.SUBMITTED,
            submitted=True,
        )
        suspend_acceptance_item(self.item)
        self.assertEqual(self.item.state, AcceptanceItem.State.SUSPENDED)
        self.assertEqual(self.item.submitted_at, submitted_at)
        self.assertIsNone(self.item.approved_at)

        resume_acceptance_item(self.item)
        self.assertEqual(self.item.state, AcceptanceItem.State.SUBMITTED)
        self.assertEqual(self.item.submitted_at, submitted_at)

    def test_approved_suspend_resume_and_withdraw_edges(self):
        """Approved suspension resumes approved; withdrawal is terminal."""
        approved_at = self.set_state(
            AcceptanceItem.State.APPROVED,
            submitted=True,
            approved=True,
        )
        submitted_at = self.item.submitted_at
        suspend_acceptance_item(self.item)
        self.assertEqual(self.item.state, AcceptanceItem.State.SUSPENDED)
        self.assertEqual(self.item.submitted_at, submitted_at)
        self.assertEqual(self.item.approved_at, approved_at)

        resume_acceptance_item(self.item)
        self.assertEqual(self.item.state, AcceptanceItem.State.APPROVED)
        withdraw_acceptance_item(self.item)
        self.assertEqual(self.item.state, AcceptanceItem.State.WITHDRAWN)
        self.assertEqual(self.item.submitted_at, submitted_at)
        self.assertEqual(self.item.approved_at, approved_at)

    def test_every_unsupported_service_edge_is_rejected(self):
        """Each service rejects every source state outside its legal edges."""
        all_states = {
            AcceptanceItem.State.DRAFT,
            AcceptanceItem.State.SUBMITTED,
            AcceptanceItem.State.APPROVED,
            AcceptanceItem.State.SUSPENDED,
            AcceptanceItem.State.WITHDRAWN,
        }
        cases = (
            (
                submit_acceptance_item_for_approval,
                {AcceptanceItem.State.DRAFT},
            ),
            (
                pull_back_acceptance_item,
                {AcceptanceItem.State.SUBMITTED},
            ),
            (
                approve_acceptance_item,
                {AcceptanceItem.State.SUBMITTED},
            ),
            (
                suspend_acceptance_item,
                {
                    AcceptanceItem.State.SUBMITTED,
                    AcceptanceItem.State.APPROVED,
                },
            ),
            (
                resume_acceptance_item,
                {AcceptanceItem.State.SUSPENDED},
            ),
            (
                withdraw_acceptance_item,
                {AcceptanceItem.State.APPROVED},
            ),
        )
        for service, legal_sources in cases:
            for source_state in all_states - legal_sources:
                with self.subTest(
                    service=service.__name__,
                    source_state=source_state,
                ):
                    self.set_state(
                        source_state,
                        submitted=source_state
                        != AcceptanceItem.State.DRAFT,
                        approved=source_state
                        in {
                            AcceptanceItem.State.APPROVED,
                            AcceptanceItem.State.WITHDRAWN,
                        },
                    )
                    with self.assertRaises(InvalidTransition):
                        service(self.item)
                    self.item.refresh_from_db()
                    self.assertEqual(self.item.state, source_state)

    def test_subset_batch_operations_leave_unselected_items_unchanged(self):
        """Submission and approval batch services affect only their subset."""
        items = list(self.project.acceptance_items.order_by("order"))
        submit_acceptance_items_for_approval([items[0]])
        items[0].refresh_from_db()
        items[1].refresh_from_db()
        self.assertEqual(items[0].state, AcceptanceItem.State.SUBMITTED)
        self.assertEqual(items[1].state, AcceptanceItem.State.DRAFT)

        approve_acceptance_items([items[0]])
        items[0].refresh_from_db()
        items[1].refresh_from_db()
        self.assertEqual(items[0].state, AcceptanceItem.State.APPROVED)
        self.assertEqual(items[1].state, AcceptanceItem.State.DRAFT)

    def test_item_lock_is_false_only_for_draft(self):
        """Only draft criteria are editable through the per-item API."""
        for state in AcceptanceItem.State.values:
            with self.subTest(state=state):
                self.set_state(state)
                self.assertEqual(
                    acceptance_item_locked(self.item),
                    state != AcceptanceItem.State.DRAFT,
                )

    def test_delete_works_in_every_mutable_project_status(self):
        """Never-approved scope remains deletable before delivery."""
        for status in (
            Project.Status.DRAFT,
            Project.Status.CRITERIA_PENDING,
            Project.Status.ACTIVE,
        ):
            with self.subTest(status=status):
                project = make_project_with_items(status=status)
                item = project.acceptance_items.order_by("order").first()
                item.state = AcceptanceItem.State.DRAFT
                item.submitted_at = None
                item.approved_at = None
                item.save(
                    update_fields=("state", "submitted_at", "approved_at"),
                )
                item_pk = item.pk

                delete_acceptance_item(item)

                self.assertFalse(
                    AcceptanceItem.objects.filter(pk=item_pk).exists()
                )

    def test_delete_refuses_every_frozen_project_status(self):
        """Never-approved scope cannot be deleted from delivery onward."""
        for status in (
            Project.Status.DELIVERED,
            Project.Status.ATTESTED,
            Project.Status.DISPUTED,
        ):
            with self.subTest(status=status):
                project = make_project_with_items(status=status)
                item = project.acceptance_items.order_by("order").first()
                item.state = AcceptanceItem.State.SUSPENDED
                item.approved_at = None
                item.save(update_fields=("state", "approved_at"))

                with self.assertRaisesMessage(
                    InvalidTransition,
                    FROZEN_SCOPE_DELETE_REFUSAL,
                ):
                    delete_acceptance_item(item)

                self.assertTrue(
                    AcceptanceItem.objects.filter(pk=item.pk).exists()
                )

    def test_delete_refusal_message_identifies_approved_history(self):
        """Approved history reports its own rule instead of the scope freeze."""
        approved_item = self.project.acceptance_items.order_by("order").last()
        approved_item.state = AcceptanceItem.State.SUSPENDED
        approved_item.submitted_at = timezone.now()
        approved_item.approved_at = timezone.now()
        approved_item.save(
            update_fields=("state", "submitted_at", "approved_at"),
        )
        with self.assertRaisesMessage(
            InvalidTransition,
            APPROVED_ITEM_DELETE_REFUSAL,
        ):
            delete_acceptance_item(approved_item)
        self.assertTrue(
            AcceptanceItem.objects.filter(pk=approved_item.pk).exists()
        )

    def test_successful_delete_keeps_guards_in_final_database_delete(self):
        """Child cascade queries do not move guards out of the item DELETE."""
        item_pk = self.item.pk
        delete_statements = []

        def capture_delete(execute, sql, params, many, context):
            if sql.lstrip().lower().startswith("delete"):
                delete_statements.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture_delete):
            delete_acceptance_item(self.item)

        self.assertEqual(len(delete_statements), 2)
        _, guarded_params = delete_statements[-1]
        self.assertCountEqual(
            guarded_params,
            (
                item_pk,
                Project.Status.DRAFT,
                Project.Status.CRITERIA_PENDING,
                Project.Status.ACTIVE,
            ),
        )
        self.assertFalse(
            AcceptanceItem.objects.filter(pk=item_pk).exists()
        )

    def test_delete_rechecks_database_status_after_stale_related_status_read(self):
        """A stale active relation cannot bypass the current delivery freeze."""
        project = make_project_with_items(status=Project.Status.ACTIVE)
        project.acceptance_items.update(is_passed=True)
        parked_item = project.acceptance_items.order_by("order").last()
        parked_item.state = AcceptanceItem.State.SUSPENDED
        parked_item.approved_at = None
        parked_item.save(update_fields=("state", "approved_at"))
        stale_item = AcceptanceItem.objects.select_related("project").get(
            pk=parked_item.pk
        )
        self.assertEqual(stale_item.project.status, Project.Status.ACTIVE)

        competing_project = Project.objects.get(pk=project.pk)
        mark_delivered(competing_project)
        with self.assertRaisesMessage(
            InvalidTransition,
            FROZEN_SCOPE_DELETE_REFUSAL,
        ):
            delete_acceptance_item(stale_item)

        self.assertTrue(
            AcceptanceItem.objects.filter(pk=parked_item.pk).exists()
        )

    def test_project_level_bridge_reaches_attested_with_default_items(self):
        """The untouched Surface-shaped project flow remains end-to-end valid."""
        project = Project.objects.create(
            owner=make_profile(),
            title="Compatibility project",
            client_name="Compatibility Client",
            client_email="compatibility@example.com",
            brief="Exercise the pre-I1b path",
        )
        for order in (1, 2):
            AcceptanceItem.objects.create(
                project=project,
                text=f"Compatibility criterion {order}",
                order=order,
            )
        self.assertTrue(
            all(
                item.state == AcceptanceItem.State.DRAFT
                for item in project.acceptance_items.all()
            )
        )

        submit_criteria_for_approval(project)
        self.assertFalse(
            project.acceptance_items.exclude(
                state=AcceptanceItem.State.SUBMITTED,
            ).exists()
        )
        approve_criteria(project)
        self.assertFalse(
            project.acceptance_items.exclude(
                state=AcceptanceItem.State.APPROVED,
            ).exists()
        )
        mark_all_items_passed(project)
        mark_delivered(project)
        sign_attestation(
            project,
            "signer@acme.com",
            "Signer",
            safe_signature_meta(),
        )
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.ATTESTED)

    def test_submit_criteria_rollback_on_invalid_transition(self):
        """Bulk item submit rolls back when the project transition fails."""
        project = make_project_with_items(status=Project.Status.ACTIVE)
        items = list(project.acceptance_items.order_by("order"))
        items[1].state = AcceptanceItem.State.DRAFT
        items[1].submitted_at = None
        items[1].approved_at = None
        items[1].save(
            update_fields=("state", "submitted_at", "approved_at"),
        )
        before = {
            item.pk: (item.state, item.submitted_at, item.approved_at)
            for item in project.acceptance_items.order_by("order")
        }

        with self.assertRaises(InvalidTransition):
            submit_criteria_for_approval(project)

        for item in project.acceptance_items.order_by("order"):
            self.assertEqual(
                (item.state, item.submitted_at, item.approved_at),
                before[item.pk],
            )
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.ACTIVE)

    def test_approve_criteria_rollback_on_invalid_transition(self):
        """Bulk item approval rolls back when the project transition fails."""
        project = make_project_with_items(status=Project.Status.DRAFT)
        timestamp = timezone.now()
        before = {}
        for item in project.acceptance_items.order_by("order"):
            item.state = AcceptanceItem.State.SUBMITTED
            item.submitted_at = timestamp
            item.approved_at = None
            item.save(
                update_fields=("state", "submitted_at", "approved_at"),
            )
            before[item.pk] = (
                item.state,
                item.submitted_at,
                item.approved_at,
            )

        with self.assertRaises(InvalidTransition):
            approve_criteria(project)

        for item in project.acceptance_items.order_by("order"):
            self.assertEqual(
                (item.state, item.submitted_at, item.approved_at),
                before[item.pk],
            )
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.DRAFT)


class AcceptanceStepServiceTests(TestCase):
    """Pin step structure and progress guards at both bounds."""

    def make_item(self, state=AcceptanceItem.State.DRAFT, project_status=None):
        """Create and return one item in an explicit state and project status."""
        if project_status is None:
            project_status = Project.Status.DRAFT
        project = make_project_with_items(status=project_status)
        item = project.acceptance_items.order_by("order").first()
        item.state = state
        item.save(update_fields=("state",))
        return project, item

    def test_draft_structure_services_create_update_and_delete(self):
        """All three structural mutations succeed for a draft parent."""
        _, item = self.make_item()

        step = create_acceptance_step(item, "First version", 1)
        self.assertFalse(acceptance_step_locked(step))
        update_acceptance_step(step, "Revised version", 2)
        step.refresh_from_db()
        self.assertEqual((step.text, step.order), ("Revised version", 2))

        step_pk = step.pk
        delete_acceptance_step(step)
        self.assertFalse(AcceptanceStep.objects.filter(pk=step_pk).exists())

    def test_structure_services_refuse_every_non_draft_item_state(self):
        """Create, update, and delete all refuse every locked parent state."""
        refusal_messages = {
            "create": (
                "Acceptance steps can only be created while their item is draft."
            ),
            "update": (
                "Acceptance steps can only be changed while their item is draft."
            ),
            "delete": (
                "Acceptance steps can only be deleted while their item is draft."
            ),
        }
        for state in AcceptanceItem.State.values:
            if state == AcceptanceItem.State.DRAFT:
                continue
            for mutation_name, refusal_message in refusal_messages.items():
                with self.subTest(state=state, mutation=mutation_name):
                    _, item = self.make_item(state=state)
                    step = AcceptanceStep.objects.create(
                        item=item,
                        text="Locked step",
                        order=1,
                    )

                    with self.assertRaisesMessage(
                        InvalidTransition,
                        refusal_message,
                    ):
                        if mutation_name == "create":
                            create_acceptance_step(item, "New step", 2)
                        elif mutation_name == "update":
                            update_acceptance_step(step, "Changed step", 2)
                        else:
                            delete_acceptance_step(step)

                    step.refresh_from_db()
                    self.assertEqual((step.text, step.order), ("Locked step", 1))
                    self.assertTrue(acceptance_step_locked(step))

    def test_structure_services_reject_blank_text(self):
        """Creation and update reject text that is blank after stripping."""
        _, item = self.make_item()
        step = create_acceptance_step(item, "Existing", 1)

        with self.assertRaises(ValueError):
            create_acceptance_step(item, " \t ", 2)
        with self.assertRaises(ValueError):
            update_acceptance_step(step, "\n", 2)

        step.refresh_from_db()
        self.assertEqual((step.text, step.order), ("Existing", 1))

    def test_duplicate_order_raises_integrity_error(self):
        """The database forbids duplicate step positions under one item."""
        _, item = self.make_item()
        create_acceptance_step(item, "First", 1)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                create_acceptance_step(item, "Duplicate", 1)

    def test_structural_successes_each_use_one_guarded_statement(self):
        """Update and delete each keep the parent-state guard in one query."""
        _, item = self.make_item()
        update_step = create_acceptance_step(item, "Update me", 1)

        with CaptureQueriesContext(connection) as update_queries:
            update_acceptance_step(update_step, "Updated", 2)
        self.assertEqual(len(update_queries), 1)
        self.assertIn("ledger_acceptanceitem", update_queries[0]["sql"].lower())

        delete_step = create_acceptance_step(item, "Delete me", 1)
        with CaptureQueriesContext(connection) as delete_queries:
            delete_acceptance_step(delete_step)
        self.assertEqual(len(delete_queries), 1)
        self.assertIn("ledger_acceptanceitem", delete_queries[0]["sql"].lower())

    def test_parent_bulk_delete_cascades_to_steps(self):
        """Draft-item bulk replacement cannot leave orphaned step rows."""
        _, item = self.make_item()
        step = create_acceptance_step(item, "Removed with parent", 1)

        AcceptanceItem.objects.filter(pk=item.pk).delete()

        self.assertFalse(AcceptanceStep.objects.filter(pk=step.pk).exists())

    def test_manual_item_delete_handles_every_related_model(self):
        """Raw item deletion must track child relations and delete receivers."""
        raw_delete_warning = (
            "AcceptanceItem's child relations, deletion behavior, or delete "
            "signal listeners changed. delete_acceptance_item uses _raw_delete, "
            "so update its manual child deletion and account for pre_delete or "
            "post_delete receivers; _raw_delete will not dispatch them."
        )
        cascading_relations = {
            (
                relation.related_model,
                relation.field.remote_field.on_delete,
            )
            for relation in AcceptanceItem._meta.related_objects
        }

        self.assertEqual(
            cascading_relations,
            {(AcceptanceStep, models.CASCADE)},
            msg=raw_delete_warning,
        )
        registered_delete_signals = {
            signal_name
            for signal_name, signal in (
                ("pre_delete", pre_delete),
                ("post_delete", post_delete),
            )
            if signal.has_listeners(AcceptanceItem)
        }
        self.assertEqual(
            registered_delete_signals,
            set(),
            msg=raw_delete_warning,
        )

    def test_is_done_allowed_only_for_approved_item_on_active_project(self):
        """The full item-state/project-status matrix pins both guard bounds."""
        for project_status in Project.Status.values:
            for item_state in AcceptanceItem.State.values:
                with self.subTest(
                    project_status=project_status,
                    item_state=item_state,
                ):
                    _, item = self.make_item(
                        state=item_state,
                        project_status=project_status,
                    )
                    step = AcceptanceStep.objects.create(
                        item=item,
                        text="Track progress",
                        order=1,
                    )
                    is_permitted = (
                        project_status == Project.Status.ACTIVE
                        and item_state == AcceptanceItem.State.APPROVED
                    )

                    if is_permitted:
                        set_acceptance_step_done(step, True)
                    else:
                        with self.assertRaises(InvalidTransition):
                            set_acceptance_step_done(step, True)

                    step.refresh_from_db()
                    self.assertEqual(step.is_done, is_permitted)

    def test_set_done_requires_a_boolean(self):
        """Progress rejects truthy non-boolean values before writing."""
        _, item = self.make_item(
            state=AcceptanceItem.State.APPROVED,
            project_status=Project.Status.ACTIVE,
        )
        step = AcceptanceStep.objects.create(
            item=item,
            text="Typed progress",
            order=1,
        )

        with self.assertRaises(ValueError):
            set_acceptance_step_done(step, 1)

        step.refresh_from_db()
        self.assertFalse(step.is_done)

    def test_set_done_success_is_one_multi_hop_guarded_statement(self):
        """Item and project guards compile into the successful UPDATE."""
        _, item = self.make_item(
            state=AcceptanceItem.State.APPROVED,
            project_status=Project.Status.ACTIVE,
        )
        step = AcceptanceStep.objects.create(
            item=item,
            text="One statement",
            order=1,
        )

        with CaptureQueriesContext(connection) as captured_queries:
            set_acceptance_step_done(step, True)

        self.assertEqual(len(captured_queries), 1)
        sql = captured_queries[0]["sql"].lower()
        self.assertIn("update", sql)
        self.assertIn("ledger_acceptanceitem", sql)
        self.assertIn("ledger_project", sql)
        step.refresh_from_db()
        self.assertTrue(step.is_done)

    def test_set_done_refusal_distinguishes_item_and_project_guards(self):
        """Progress refusal identifies which current relation blocks it."""
        _, submitted_item = self.make_item(
            state=AcceptanceItem.State.SUBMITTED,
            project_status=Project.Status.ACTIVE,
        )
        submitted_step = AcceptanceStep.objects.create(
            item=submitted_item,
            text="Await approval",
            order=1,
        )
        with self.assertRaisesMessage(
            InvalidTransition,
            "Acceptance step progress requires an approved item.",
        ):
            set_acceptance_step_done(submitted_step, True)

        _, approved_item = self.make_item(
            state=AcceptanceItem.State.APPROVED,
            project_status=Project.Status.DELIVERED,
        )
        approved_step = AcceptanceStep.objects.create(
            item=approved_item,
            text="Project frozen",
            order=1,
        )
        with self.assertRaisesMessage(
            InvalidTransition,
            "Acceptance step progress can only change while the project is active.",
        ):
            set_acceptance_step_done(approved_step, True)

    def test_structural_updates_recheck_current_parent_state(self):
        """A stale draft relation cannot overwrite newly locked step content."""
        _, item = self.make_item()
        step = create_acceptance_step(item, "Original", 1)
        stale_step = AcceptanceStep.objects.select_related("item").get(pk=step.pk)
        self.assertEqual(stale_step.item.state, AcceptanceItem.State.DRAFT)
        AcceptanceItem.objects.filter(pk=item.pk).update(
            state=AcceptanceItem.State.SUBMITTED,
        )

        with self.assertRaisesMessage(
            InvalidTransition,
            "Acceptance steps can only be changed while their item is draft.",
        ):
            update_acceptance_step(stale_step, "Stale overwrite", 2)

        step.refresh_from_db()
        self.assertEqual((step.text, step.order), ("Original", 1))

    def test_structural_delete_rechecks_current_parent_state(self):
        """A stale draft relation cannot delete a newly locked step."""
        _, item = self.make_item()
        step = create_acceptance_step(item, "Keep me", 1)
        stale_step = AcceptanceStep.objects.select_related("item").get(pk=step.pk)
        self.assertEqual(stale_step.item.state, AcceptanceItem.State.DRAFT)
        AcceptanceItem.objects.filter(pk=item.pk).update(
            state=AcceptanceItem.State.SUBMITTED,
        )

        with self.assertRaisesMessage(
            InvalidTransition,
            "Acceptance steps can only be deleted while their item is draft.",
        ):
            delete_acceptance_step(stale_step)

        self.assertTrue(AcceptanceStep.objects.filter(pk=step.pk).exists())

    def test_set_done_rechecks_current_project_status(self):
        """A stale active relation cannot write progress after delivery."""
        project, item = self.make_item(
            state=AcceptanceItem.State.APPROVED,
            project_status=Project.Status.ACTIVE,
        )
        step = AcceptanceStep.objects.create(
            item=item,
            text="Freeze at delivery",
            order=1,
        )
        stale_step = AcceptanceStep.objects.select_related(
            "item__project",
        ).get(pk=step.pk)
        self.assertEqual(
            stale_step.item.project.status,
            Project.Status.ACTIVE,
        )
        Project.objects.filter(pk=project.pk).update(
            status=Project.Status.DELIVERED,
        )

        with self.assertRaises(InvalidTransition):
            set_acceptance_step_done(stale_step, True)

        step.refresh_from_db()
        self.assertFalse(step.is_done)

    def test_each_guarded_mutation_identifies_a_deleted_step(self):
        """Update, delete, and progress each distinguish a vanished row."""
        for mutation_name in ("update", "delete", "set_done"):
            with self.subTest(mutation=mutation_name):
                _, item = self.make_item(
                    state=AcceptanceItem.State.APPROVED
                    if mutation_name == "set_done"
                    else AcceptanceItem.State.DRAFT,
                    project_status=Project.Status.ACTIVE,
                )
                step = AcceptanceStep.objects.create(
                    item=item,
                    text="Soon deleted",
                    order=1,
                )
                AcceptanceStep.objects.filter(pk=step.pk).delete()

                with self.assertRaisesMessage(
                    InvalidTransition,
                    "Acceptance step no longer exists.",
                ):
                    if mutation_name == "update":
                        update_acceptance_step(step, "Changed", 2)
                    elif mutation_name == "delete":
                        delete_acceptance_step(step)
                    else:
                        set_acceptance_step_done(step, True)


class AcceptanceItemGateTests(TestCase):
    """Delivery and signing gates across every acceptance-item state."""

    def set_item_state(self, project, state, is_passed):
        """Set both fixture items to one gate scenario."""
        timestamp = timezone.now()
        project.acceptance_items.update(
            state=state,
            submitted_at=(
                None if state == AcceptanceItem.State.DRAFT else timestamp
            ),
            approved_at=(
                timestamp
                if state
                in {
                    AcceptanceItem.State.APPROVED,
                    AcceptanceItem.State.WITHDRAWN,
                }
                else None
            ),
            is_passed=is_passed,
        )

    def add_approved_peer(self, project):
        """Keep one passed approved item beside a parked-state fixture."""
        timestamp = timezone.now()
        item = project.acceptance_items.order_by("order").first()
        item.state = AcceptanceItem.State.APPROVED
        item.submitted_at = timestamp
        item.approved_at = timestamp
        item.is_passed = True
        item.save(
            update_fields=("state", "submitted_at", "approved_at", "is_passed"),
        )

    def set_blocker_item(self, project, state, is_passed):
        """Pair one approved peer with a second item in the blocker state."""
        self.add_approved_peer(project)
        timestamp = timezone.now()
        item = project.acceptance_items.order_by("order").last()
        item.state = state
        item.submitted_at = (
            None if state == AcceptanceItem.State.DRAFT else timestamp
        )
        item.approved_at = (
            timestamp
            if state
            in {
                AcceptanceItem.State.APPROVED,
                AcceptanceItem.State.WITHDRAWN,
            }
            else None
        )
        item.is_passed = is_passed
        item.save(
            update_fields=("state", "submitted_at", "approved_at", "is_passed"),
        )

    def test_delivery_blocks_draft_item_beside_approved_peer(self):
        """Delivery rejects draft items even when another item is approved."""
        project = make_project_with_items(status=Project.Status.ACTIVE)
        self.set_blocker_item(project, AcceptanceItem.State.DRAFT, None)

        with self.assertRaises(InvalidTransition) as raised:
            mark_delivered(project)

        self.assertEqual(str(raised.exception), DRAFT_SUBMITTED_BLOCK_DELIVERY)

    def test_delivery_blocks_submitted_item_beside_approved_peer(self):
        """Delivery rejects submitted items even when another item is approved."""
        project = make_project_with_items(status=Project.Status.ACTIVE)
        self.set_blocker_item(project, AcceptanceItem.State.SUBMITTED, True)

        with self.assertRaises(InvalidTransition) as raised:
            mark_delivered(project)

        self.assertEqual(str(raised.exception), DRAFT_SUBMITTED_BLOCK_DELIVERY)

    def test_signing_blocks_draft_item_beside_approved_peer(self):
        """Signing rejects draft items even when another item is approved."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        self.set_blocker_item(project, AcceptanceItem.State.DRAFT, True)

        with self.assertRaises(InvalidTransition) as raised:
            sign_attestation(
                project,
                "signer@acme.com",
                "Signer",
                safe_signature_meta(),
            )

        self.assertEqual(str(raised.exception), DRAFT_SUBMITTED_BLOCK_SIGNING)

    def test_signing_blocks_submitted_item_beside_approved_peer(self):
        """Signing rejects submitted items even when another item is approved."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        self.set_blocker_item(project, AcceptanceItem.State.SUBMITTED, True)

        with self.assertRaises(InvalidTransition) as raised:
            sign_attestation(
                project,
                "signer@acme.com",
                "Signer",
                safe_signature_meta(),
            )

        self.assertEqual(str(raised.exception), DRAFT_SUBMITTED_BLOCK_SIGNING)

    def test_delivery_gate_covers_all_five_states(self):
        """Draft/submitted and failed approved block; parked states do not."""
        cases = (
            (AcceptanceItem.State.DRAFT, None, True),
            (AcceptanceItem.State.SUBMITTED, True, True),
            (AcceptanceItem.State.APPROVED, False, True),
            (AcceptanceItem.State.SUSPENDED, None, False),
            (AcceptanceItem.State.WITHDRAWN, False, False),
        )
        for state, is_passed, should_block in cases:
            with self.subTest(state=state):
                project = make_project_with_items(
                    status=Project.Status.ACTIVE,
                )
                if state in {
                    AcceptanceItem.State.DRAFT,
                    AcceptanceItem.State.SUBMITTED,
                    AcceptanceItem.State.SUSPENDED,
                    AcceptanceItem.State.WITHDRAWN,
                }:
                    self.set_blocker_item(project, state, is_passed)
                    expected_message = (
                        DRAFT_SUBMITTED_BLOCK_DELIVERY
                        if state
                        in {
                            AcceptanceItem.State.DRAFT,
                            AcceptanceItem.State.SUBMITTED,
                        }
                        else None
                    )
                else:
                    self.set_item_state(project, state, is_passed)
                    expected_message = DELIVERY_FAILED_ITEM
                if should_block:
                    with self.assertRaises(InvalidTransition) as raised:
                        mark_delivered(project)
                    self.assertEqual(str(raised.exception), expected_message)
                else:
                    mark_delivered(project)
                    project.refresh_from_db()
                    self.assertEqual(
                        project.status,
                        Project.Status.DELIVERED,
                    )

    def test_approved_null_result_preserves_delivery_null_semantics(self):
        """An approved undecided result remains non-blocking for delivery."""
        project = make_project_with_items(status=Project.Status.ACTIVE)
        self.set_item_state(
            project,
            AcceptanceItem.State.APPROVED,
            None,
        )
        mark_delivered(project)
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.DELIVERED)

    def test_signing_gate_covers_all_five_states(self):
        """Only passed approved items attest; parked states are ignored."""
        cases = (
            (AcceptanceItem.State.DRAFT, True, True),
            (AcceptanceItem.State.SUBMITTED, True, True),
            (AcceptanceItem.State.APPROVED, True, False),
            (AcceptanceItem.State.SUSPENDED, None, False),
            (AcceptanceItem.State.WITHDRAWN, False, False),
        )
        for state, is_passed, should_block in cases:
            with self.subTest(state=state):
                project = make_project_with_items(
                    status=Project.Status.DELIVERED,
                )
                if state in {
                    AcceptanceItem.State.DRAFT,
                    AcceptanceItem.State.SUBMITTED,
                    AcceptanceItem.State.SUSPENDED,
                    AcceptanceItem.State.WITHDRAWN,
                }:
                    self.set_blocker_item(project, state, is_passed)
                    expected_message = (
                        DRAFT_SUBMITTED_BLOCK_SIGNING
                        if state
                        in {
                            AcceptanceItem.State.DRAFT,
                            AcceptanceItem.State.SUBMITTED,
                        }
                        else None
                    )
                else:
                    self.set_item_state(project, state, is_passed)
                    expected_message = None
                if should_block:
                    with self.assertRaises(InvalidTransition) as raised:
                        sign_attestation(
                            project,
                            "signer@acme.com",
                            "Signer",
                            safe_signature_meta(),
                        )
                    if state == AcceptanceItem.State.APPROVED:
                        self.assertEqual(
                            str(raised.exception),
                            SIGNING_FAILED_ITEM,
                        )
                    else:
                        self.assertEqual(
                            str(raised.exception),
                            expected_message,
                        )
                else:
                    sign_attestation(
                        project,
                        "signer@acme.com",
                        "Signer",
                        safe_signature_meta(),
                    )
                    project.refresh_from_db()
                    self.assertEqual(
                        project.status,
                        Project.Status.ATTESTED,
                    )

    def test_signing_rejects_approved_null_and_false_results(self):
        """Both undecided and failed approved items block signing."""
        for is_passed in (None, False):
            with self.subTest(is_passed=is_passed):
                project = make_project_with_items(
                    status=Project.Status.DELIVERED,
                )
                self.set_item_state(
                    project,
                    AcceptanceItem.State.APPROVED,
                    is_passed,
                )
                with self.assertRaises(InvalidTransition) as raised:
                    sign_attestation(
                        project,
                        "signer@acme.com",
                        "Signer",
                        safe_signature_meta(),
                    )
                self.assertEqual(str(raised.exception), SIGNING_FAILED_ITEM)


class VacuousSigningTests(TestCase):
    """An attestation must cover at least one approved, passed criterion."""

    def _park_all_items(self, project, *, state):
        """Move every item to a non-attesting terminal or parked state."""
        timestamp = timezone.now()
        project.acceptance_items.update(
            state=state,
            submitted_at=timestamp,
            approved_at=(
                timestamp
                if state == AcceptanceItem.State.WITHDRAWN
                else None
            ),
            is_passed=False if state == AcceptanceItem.State.WITHDRAWN else None,
        )

    def test_all_suspended_items_cannot_be_delivered_or_signed(self):
        """Suspended-only projects must not attest to zero approved criteria."""
        active = make_project_with_items(status=Project.Status.ACTIVE)
        self._park_all_items(active, state=AcceptanceItem.State.SUSPENDED)
        with self.assertRaises(InvalidTransition) as raised:
            mark_delivered(active)
        self.assertEqual(str(raised.exception), VACUOUS_DELIVERY)

        delivered = make_project_with_items(status=Project.Status.DELIVERED)
        self._park_all_items(delivered, state=AcceptanceItem.State.SUSPENDED)
        with self.assertRaises(InvalidTransition) as raised:
            sign_attestation(
                delivered,
                "signer@acme.com",
                "Signer",
                safe_signature_meta(),
            )
        self.assertEqual(str(raised.exception), VACUOUS_SIGNING)
        self.assertFalse(delivered.attestations.exists())

    def test_all_withdrawn_items_cannot_be_delivered_or_signed(self):
        """Withdrawn-only projects must not produce empty attestations."""
        active = make_project_with_items(status=Project.Status.ACTIVE)
        self._park_all_items(active, state=AcceptanceItem.State.WITHDRAWN)
        with self.assertRaises(InvalidTransition) as raised:
            mark_delivered(active)
        self.assertEqual(str(raised.exception), VACUOUS_DELIVERY)

        delivered = make_project_with_items(status=Project.Status.DELIVERED)
        self._park_all_items(delivered, state=AcceptanceItem.State.WITHDRAWN)
        with self.assertRaises(InvalidTransition) as raised:
            sign_attestation(
                delivered,
                "signer@acme.com",
                "Signer",
                safe_signature_meta(),
            )
        self.assertEqual(str(raised.exception), VACUOUS_SIGNING)
        self.assertFalse(delivered.attestations.exists())

    def test_mixed_suspended_and_withdrawn_items_cannot_be_delivered_or_signed(
        self,
    ):
        """A mix of parked states with zero approved items must still block."""
        active = make_project_with_items(status=Project.Status.ACTIVE)
        items = list(active.acceptance_items.order_by("order"))
        timestamp = timezone.now()
        items[0].state = AcceptanceItem.State.SUSPENDED
        items[0].submitted_at = timestamp
        items[0].approved_at = None
        items[0].is_passed = None
        items[0].save(
            update_fields=("state", "submitted_at", "approved_at", "is_passed"),
        )
        items[1].state = AcceptanceItem.State.WITHDRAWN
        items[1].submitted_at = timestamp
        items[1].approved_at = timestamp
        items[1].is_passed = False
        items[1].save(
            update_fields=("state", "submitted_at", "approved_at", "is_passed"),
        )
        with self.assertRaises(InvalidTransition) as raised:
            mark_delivered(active)
        self.assertEqual(str(raised.exception), VACUOUS_DELIVERY)

        delivered = make_project_with_items(status=Project.Status.DELIVERED)
        items = list(delivered.acceptance_items.order_by("order"))
        items[0].state = AcceptanceItem.State.SUSPENDED
        items[0].submitted_at = timestamp
        items[0].approved_at = None
        items[0].is_passed = None
        items[0].save(
            update_fields=("state", "submitted_at", "approved_at", "is_passed"),
        )
        items[1].state = AcceptanceItem.State.WITHDRAWN
        items[1].submitted_at = timestamp
        items[1].approved_at = timestamp
        items[1].is_passed = False
        items[1].save(
            update_fields=("state", "submitted_at", "approved_at", "is_passed"),
        )
        with self.assertRaises(InvalidTransition) as raised:
            sign_attestation(
                delivered,
                "signer@acme.com",
                "Signer",
                safe_signature_meta(),
            )
        self.assertEqual(str(raised.exception), VACUOUS_SIGNING)

    def test_zero_acceptance_items_cannot_be_delivered_or_signed(self):
        """Projects with no criteria must not reach attested status."""
        owner = make_profile()
        active = Project.objects.create(
            owner=owner,
            title="Empty active project",
            client_name="Client",
            client_email="empty-active@example.com",
            brief="No criteria",
            status=Project.Status.ACTIVE,
        )
        with self.assertRaises(InvalidTransition) as raised:
            mark_delivered(active)
        self.assertEqual(str(raised.exception), VACUOUS_DELIVERY)

        delivered = Project.objects.create(
            owner=owner,
            title="Empty delivered project",
            client_name="Client",
            client_email="empty-delivered@example.com",
            brief="No criteria",
            status=Project.Status.DELIVERED,
        )
        with self.assertRaises(InvalidTransition) as raised:
            sign_attestation(
                delivered,
                "signer@acme.com",
                "Signer",
                safe_signature_meta(),
            )
        self.assertEqual(str(raised.exception), VACUOUS_SIGNING)


class ChangeOrderServiceTests(TestCase):
    """Change-order proposal, decision, and delivery guards."""

    def setUp(self):
        """Create active work eligible for a change-order proposal."""
        self.project = make_project_with_items(status=Project.Status.ACTIVE)

    def test_propose_creates_proposed_change_order(self):
        """A valid proposal persists all requested adjustment fields."""
        change_order = propose_change_order(
            self.project,
            "Add reporting export",
            25000,
            4,
        )

        self.assertEqual(change_order.project, self.project)
        self.assertEqual(change_order.description, "Add reporting export")
        self.assertEqual(change_order.amount_cents, 25000)
        self.assertEqual(change_order.timeline_days, 4)
        self.assertEqual(change_order.status, ChangeOrder.Status.PROPOSED)
        self.assertIsNone(change_order.resolved_at)

    def test_propose_rejects_non_active_project(self):
        """Change orders cannot be proposed outside active work."""
        for status in (
            Project.Status.DRAFT,
            Project.Status.CRITERIA_PENDING,
            Project.Status.DELIVERED,
            Project.Status.ATTESTED,
            Project.Status.DISPUTED,
        ):
            with self.subTest(status=status):
                project = make_project_with_items(status=status)
                with self.assertRaises(InvalidTransition):
                    propose_change_order(project, "Extra work", 100, 1)
                self.assertEqual(project.change_orders.count(), 0)

    def test_propose_rejects_invalid_fields_without_creating_row(self):
        """Blank descriptions and negative adjustments are rejected."""
        invalid_values = (
            ("", 0, 0),
            ("   ", 0, 0),
            ("Extra work", -1, 0),
            ("Extra work", 0, -1),
        )
        for description, amount_cents, timeline_days in invalid_values:
            with self.subTest(
                description=description,
                amount_cents=amount_cents,
                timeline_days=timeline_days,
            ):
                with self.assertRaises(ValueError):
                    propose_change_order(
                        self.project,
                        description,
                        amount_cents,
                        timeline_days,
                    )
        self.assertEqual(self.project.change_orders.count(), 0)

    def test_approve_resolves_proposed_change_order(self):
        """Approval stores its decision and resolution timestamp."""
        change_order = propose_change_order(
            self.project,
            "Add reporting export",
            25000,
            4,
        )

        returned = approve_change_order(change_order)
        change_order.refresh_from_db()

        self.assertEqual(returned.pk, change_order.pk)
        self.assertEqual(change_order.status, ChangeOrder.Status.APPROVED)
        self.assertIsNotNone(change_order.resolved_at)

    def test_decline_resolves_proposed_change_order(self):
        """Decline stores its decision and resolution timestamp."""
        change_order = propose_change_order(
            self.project,
            "Add reporting export",
            25000,
            4,
        )

        returned = decline_change_order(change_order)
        change_order.refresh_from_db()

        self.assertEqual(returned.pk, change_order.pk)
        self.assertEqual(change_order.status, ChangeOrder.Status.DECLINED)
        self.assertIsNotNone(change_order.resolved_at)

    def test_resolved_change_order_cannot_be_decided_again(self):
        """Only a proposed change order may receive a decision."""
        approved = propose_change_order(self.project, "Approved work", 100, 1)
        declined = propose_change_order(self.project, "Declined work", 200, 2)
        approve_change_order(approved)
        decline_change_order(declined)

        for change_order, decision in (
            (approved, approve_change_order),
            (approved, decline_change_order),
            (declined, approve_change_order),
            (declined, decline_change_order),
        ):
            with self.subTest(
                status=change_order.status,
                decision=decision.__name__,
            ):
                with self.assertRaises(InvalidTransition):
                    decision(change_order)

    def test_proposed_change_order_blocks_delivery(self):
        """Active work remains active until every proposal is decided."""
        propose_change_order(self.project, "Pending work", 100, 1)

        self.assertTrue(has_open_change_orders(self.project))
        with self.assertRaises(InvalidTransition):
            mark_delivered(self.project)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.ACTIVE)

    def test_resolved_change_orders_allow_delivery(self):
        """Approved and declined orders do not block delivery."""
        approved = propose_change_order(self.project, "Approved work", 100, 1)
        declined = propose_change_order(self.project, "Declined work", 200, 2)
        approve_change_order(approved)
        decline_change_order(declined)

        self.assertFalse(has_open_change_orders(self.project))
        mark_delivered(self.project)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.DELIVERED)

    def test_proposed_change_order_blocks_delivery_with_item_state_gate(self):
        """Open change orders still block delivery when items are approved."""
        mark_all_items_passed(self.project)
        propose_change_order(self.project, "Pending work", 100, 1)

        with self.assertRaises(InvalidTransition):
            mark_delivered(self.project)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.ACTIVE)

    def test_approved_change_order_and_item_state_both_enter_signed_payload(self):
        """Approved change orders and per-item state coexist in the payload."""
        change_order = propose_change_order(
            self.project,
            "Approved export work",
            15000,
            3,
        )
        approve_change_order(change_order)
        mark_all_items_passed(self.project)
        mark_delivered(self.project)
        items = list(self.project.acceptance_items.order_by("order"))
        items[1].state = AcceptanceItem.State.WITHDRAWN
        items[1].save(update_fields=("state",))

        attestation = sign_attestation(
            self.project,
            "signer@acme.com",
            "Signer",
            safe_signature_meta(),
        )

        payload = attestation.payload
        self.assertEqual(
            payload["approved_change_orders"],
            [
                {
                    "description": "Approved export work",
                    "amount_cents": 15000,
                }
            ],
        )
        payload_items = {
            item["text"]: item for item in payload["acceptance_items"]
        }
        self.assertEqual(payload_items["Criterion one"]["state"], "approved")
        self.assertEqual(payload_items["Criterion two"]["state"], "withdrawn")


class PayloadHashTests(TestCase):
    """Deterministic hashing and canonical payload stability."""

    def test_compute_payload_hash_is_deterministic(self):
        """Same payload dict always yields the same SHA-256 hex digest."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        mark_all_items_passed(project)
        payload = canonical_payload(project)
        first = compute_payload_hash(payload)
        second = compute_payload_hash(payload)
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")

    def test_golden_hash_matches_sorted_canonical_json(self):
        """A literal digest pins the complete canonical payload shape."""
        project = Project.objects.create(
            owner=make_profile(),
            title="Golden Project",
            client_name="Golden Client",
            client_email="golden@example.com",
            brief="Fixed brief",
            skills_csv="Python, Django",
            status=Project.Status.DELIVERED,
        )
        item = AcceptanceItem.objects.create(
            project=project,
            text="A",
            order=1,
            state=AcceptanceItem.State.APPROVED,
            submitted_at=datetime(
                2026,
                1,
                2,
                3,
                4,
                5,
                tzinfo=datetime_timezone.utc,
            ),
            approved_at=datetime(
                2026,
                1,
                3,
                4,
                5,
                6,
                tzinfo=datetime_timezone.utc,
            ),
            is_passed=True,
        )
        AcceptanceStep.objects.create(
            item=item,
            text="Second signed step",
            order=2,
            is_done=False,
        )
        AcceptanceStep.objects.create(
            item=item,
            text="First signed step",
            order=1,
            is_done=True,
        )

        self.assertEqual(
            compute_payload_hash(canonical_payload(project)),
            "e46944690ba1b2810afdc3cd31689e139fcd8b4a9e039586c8ee159ea13e1b8c",
        )

    def test_canonical_payload_serializes_item_state_and_timestamps(self):
        """Per-item datetimes become ISO strings and null remains JSON-safe."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        items = list(project.acceptance_items.order_by("order"))
        fixed_submitted_at = datetime(
            2026,
            2,
            3,
            4,
            5,
            6,
            tzinfo=datetime_timezone.utc,
        )
        fixed_approved_at = datetime(
            2026,
            2,
            4,
            5,
            6,
            7,
            tzinfo=datetime_timezone.utc,
        )
        items[0].submitted_at = fixed_submitted_at
        items[0].approved_at = fixed_approved_at
        items[0].save(update_fields=("submitted_at", "approved_at"))
        items[1].state = AcceptanceItem.State.SUSPENDED
        items[1].submitted_at = fixed_submitted_at
        items[1].approved_at = None
        items[1].save(
            update_fields=("state", "submitted_at", "approved_at"),
        )

        payload_items = canonical_payload(project)["acceptance_items"]
        self.assertEqual(
            payload_items[0],
            {
                "text": "Criterion one",
                "is_passed": None,
                "evidence_url": "",
                "state": "approved",
                "submitted_at": "2026-02-03T04:05:06+00:00",
                "approved_at": "2026-02-04T05:06:07+00:00",
                "steps": [],
            },
        )
        self.assertEqual(payload_items[1]["state"], "suspended")
        self.assertEqual(
            payload_items[1]["submitted_at"],
            "2026-02-03T04:05:06+00:00",
        )
        self.assertIsNone(payload_items[1]["approved_at"])

    def test_canonical_payload_nests_ordered_steps_in_two_queries(self):
        """Steps nest by parent in order without an item-by-item query."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        items = list(project.acceptance_items.order_by("order"))
        AcceptanceStep.objects.create(
            item=items[0],
            text="Later",
            order=2,
            is_done=False,
        )
        AcceptanceStep.objects.create(
            item=items[0],
            text="Earlier",
            order=1,
            is_done=True,
        )
        AcceptanceStep.objects.create(
            item=items[1],
            text="Other item",
            order=1,
            is_done=False,
        )

        with CaptureQueriesContext(connection) as captured_queries:
            payload_items = canonical_payload(project)["acceptance_items"]

        self.assertEqual(len(captured_queries), 3)
        acceptance_queries = [
            query["sql"]
            for query in captured_queries
            if "ledger_acceptance" in query["sql"].lower()
        ]
        self.assertEqual(len(acceptance_queries), 2)
        self.assertEqual(
            payload_items[0]["steps"],
            [
                {"text": "Earlier", "is_done": True},
                {"text": "Later", "is_done": False},
            ],
        )
        self.assertEqual(
            payload_items[1]["steps"],
            [{"text": "Other item", "is_done": False}],
        )

    def test_hash_changes_when_step_content_or_progress_changes(self):
        """Both signed step fields contribute to the canonical digest."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        item = project.acceptance_items.order_by("order").first()
        step = AcceptanceStep.objects.create(
            item=item,
            text="Signed wording",
            order=1,
            is_done=False,
        )
        original_hash = compute_payload_hash(canonical_payload(project))

        step.text = "Different signed wording"
        step.save(update_fields=("text",))
        text_hash = compute_payload_hash(canonical_payload(project))
        step.is_done = True
        step.save(update_fields=("is_done",))
        progress_hash = compute_payload_hash(canonical_payload(project))

        self.assertNotEqual(original_hash, text_hash)
        self.assertNotEqual(text_hash, progress_hash)

    def test_key_insertion_order_does_not_change_hash(self):
        """Two dicts with identical content but different key order hash equal."""
        ordered_one = {
            "title": "Same Project",
            "brief": "Same brief",
            "revision_limit": 2,
        }
        ordered_two = {
            "revision_limit": 2,
            "brief": "Same brief",
            "title": "Same Project",
        }
        self.assertEqual(
            compute_payload_hash(ordered_one),
            compute_payload_hash(ordered_two),
        )

    def test_canonical_payload_stable_across_two_calls(self):
        """Repeated serialization of unchanged project data is identical."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        mark_all_items_passed(project)
        first = canonical_payload(project)
        second = canonical_payload(project)
        self.assertEqual(first, second)

    def test_hash_changes_when_acceptance_item_is_passed_changes(self):
        """Mutating verification state changes the payload hash."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        item = project.acceptance_items.order_by("order").first()
        item.is_passed = True
        item.save(update_fields=("is_passed",))
        hash_partial = compute_payload_hash(canonical_payload(project))
        item.is_passed = False
        item.save(update_fields=("is_passed",))
        hash_reversed = compute_payload_hash(canonical_payload(project))
        self.assertNotEqual(hash_partial, hash_reversed)

    def test_sign_stores_payload_hash_matching_independent_recomputation(self):
        """Stored hash equals a fresh canonical_payload + compute_payload_hash."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        mark_all_items_passed(project)
        attestation = sign_attestation(
            project,
            "signer@acme.com",
            "Signer",
            safe_signature_meta(),
        )
        recomputed = compute_payload_hash(canonical_payload(project))
        self.assertEqual(attestation.payload_hash, recomputed)

    def test_signed_step_snapshot_does_not_follow_live_row_changes(self):
        """Changing a live step cannot rewrite an existing signed payload."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        mark_all_items_passed(project)
        item = project.acceptance_items.order_by("order").first()
        step = AcceptanceStep.objects.create(
            item=item,
            text="Signed step",
            order=1,
            is_done=False,
        )
        attestation = sign_attestation(
            project,
            "signer@acme.com",
            "Signer",
            safe_signature_meta(),
        )
        self.assertEqual(
            attestation.payload["acceptance_items"][0]["steps"],
            [{"text": "Signed step", "is_done": False}],
        )
        signed_payload = json.loads(json.dumps(attestation.payload))
        signed_hash = attestation.payload_hash

        AcceptanceStep.objects.filter(pk=step.pk).update(
            text="Later live value",
            is_done=True,
        )
        attestation.refresh_from_db()

        self.assertEqual(attestation.payload, signed_payload)
        self.assertEqual(attestation.payload_hash, signed_hash)
        self.assertTrue(verify_payload_hash(attestation))


class SignAttestationTests(TestCase):
    """Guards, side effects, and atomicity of sign_attestation."""

    def test_raises_when_approved_item_is_unverified(self):
        """An approved item with is_passed=None blocks signing."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        items = list(project.acceptance_items.order_by("order"))
        items[0].is_passed = True
        items[0].save(update_fields=("is_passed",))
        with self.assertRaises(InvalidTransition) as raised:
            sign_attestation(
                project,
                "signer@acme.com",
                "Signer",
                safe_signature_meta(),
            )
        self.assertEqual(str(raised.exception), SIGNING_FAILED_ITEM)

    def test_raises_when_approved_item_failed(self):
        """An approved item with is_passed=False blocks signing."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        items = list(project.acceptance_items.order_by("order"))
        items[0].is_passed = True
        items[0].save(update_fields=("is_passed",))
        items[1].is_passed = False
        items[1].save(update_fields=("is_passed",))

        with self.assertRaises(InvalidTransition) as raised:
            sign_attestation(
                project,
                "signer@acme.com",
                "Signer",
                safe_signature_meta(),
            )
        self.assertEqual(str(raised.exception), SIGNING_FAILED_ITEM)

        self.assertFalse(project.attestations.exists())

    def test_requires_delivered_status(self):
        """Only delivered projects may be signed."""
        project = make_project_with_items(status=Project.Status.ACTIVE)
        mark_all_items_passed(project)
        with self.assertRaises(InvalidTransition):
            sign_attestation(
                project,
                "signer@acme.com",
                "Signer",
                safe_signature_meta(),
            )

    def test_creates_attestation_with_is_current_true(self):
        """New signature row is marked as the current attestation."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        mark_all_items_passed(project)
        attestation = sign_attestation(
            project,
            "signer@acme.com",
            "Signer",
            safe_signature_meta(),
        )
        self.assertTrue(attestation.is_current)

    def test_project_ends_attested_after_sign(self):
        """Successful signing moves project status to attested."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        mark_all_items_passed(project)
        sign_attestation(
            project,
            "signer@acme.com",
            "Signer",
            safe_signature_meta(),
        )
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.ATTESTED)

    def test_unsupported_signature_meta_key_raises(self):
        """A raw client IP key in signature metadata is rejected."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        mark_all_items_passed(project)
        with self.assertRaises(InvalidSignatureMeta):
            sign_attestation(
                project,
                "signer@acme.com",
                "Signer",
                {"ip": "1.2.3.4"},
            )
        self.assertEqual(project.attestations.count(), 0)

    def test_allowed_signature_meta_keys_stored_intact(self):
        """user_agent, ip_hash, and ip_truncated persist unchanged."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        mark_all_items_passed(project)
        meta = {
            "user_agent": "TestAgent/1.0",
            "ip_hash": "abc123def456",
            "ip_truncated": "203.0.113.0",
        }
        attestation = sign_attestation(
            project,
            "signer@acme.com",
            "Signer",
            meta,
        )
        attestation.refresh_from_db()
        self.assertEqual(attestation.signature_meta, meta)

    def test_failed_sign_leaves_no_attestation_row(self):
        """A rejected sign rolls back without persisting an attestation."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        with self.assertRaises(InvalidTransition):
            sign_attestation(
                project,
                "signer@acme.com",
                "Signer",
                safe_signature_meta(),
            )
        self.assertEqual(project.attestations.count(), 0)

    def test_suspended_item_does_not_block_signing_when_other_items_pass(self):
        """One suspended item must not block signing for passed approved peers."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        items = list(project.acceptance_items.order_by("order"))
        items[0].is_passed = True
        items[0].save(update_fields=("is_passed",))
        items[1].state = AcceptanceItem.State.SUSPENDED
        items[1].submitted_at = timezone.now()
        items[1].approved_at = None
        items[1].is_passed = None
        items[1].save(
            update_fields=("state", "submitted_at", "approved_at", "is_passed"),
        )

        attestation = sign_attestation(
            project,
            "signer@acme.com",
            "Signer",
            safe_signature_meta(),
        )

        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.ATTESTED)
        payload_items = {
            item["text"]: item for item in attestation.payload["acceptance_items"]
        }
        self.assertEqual(payload_items["Criterion one"]["state"], "approved")
        self.assertEqual(payload_items["Criterion two"]["state"], "suspended")

    def test_withdrawn_item_remains_in_signed_payload_with_state(self):
        """Withdrawn criteria stay in the signed record instead of vanishing."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        items = list(project.acceptance_items.order_by("order"))
        items[0].is_passed = True
        items[0].save(update_fields=("is_passed",))
        items[1].state = AcceptanceItem.State.WITHDRAWN
        items[1].is_passed = False
        items[1].save(update_fields=("state", "is_passed"))

        attestation = sign_attestation(
            project,
            "signer@acme.com",
            "Signer",
            safe_signature_meta(),
        )

        payload_items = attestation.payload["acceptance_items"]
        self.assertEqual(len(payload_items), 2)
        withdrawn_rows = [
            item for item in payload_items if item["state"] == "withdrawn"
        ]
        self.assertEqual(len(withdrawn_rows), 1)
        self.assertEqual(withdrawn_rows[0]["text"], "Criterion two")


class AttestationImmutabilityTests(TestCase):
    """Signed attestation rows reject mutation of immutable fields."""

    def setUp(self):
        """Sign one project so immutability tests have a real attestation."""
        self.project = make_project_with_items(status=Project.Status.DELIVERED)
        mark_all_items_passed(self.project)
        self.attestation = sign_attestation(
            self.project,
            "signer@acme.com",
            "Signer Name",
            safe_signature_meta(),
        )

    def test_save_raises_when_payload_hash_changed(self):
        """Instance save blocks payload_hash mutation."""
        self.attestation.payload_hash = "0" * 64
        with self.assertRaises(ImmutableAttestation):
            self.attestation.save()

    def test_save_raises_when_payload_changed(self):
        """Instance save blocks payload mutation."""
        self.attestation.payload = {"tampered": True}
        with self.assertRaises(ImmutableAttestation):
            self.attestation.save()

    def test_save_raises_when_client_email_changed(self):
        """Instance save blocks client_email mutation."""
        self.attestation.client_email = "other@example.com"
        with self.assertRaises(ImmutableAttestation):
            self.attestation.save()

    def test_save_raises_when_client_name_typed_changed(self):
        """Instance save blocks client_name_typed mutation."""
        self.attestation.client_name_typed = "Other Name"
        with self.assertRaises(ImmutableAttestation):
            self.attestation.save()

    def test_save_raises_when_signed_at_changed(self):
        """Instance save blocks signed_at mutation."""
        # Derive the new value from the stored one instead of reading the clock
        # again: a same-tick timezone.now() can equal signed_at, leaving the row
        # unmutated and the guard with nothing to reject.
        self.attestation.signed_at -= timedelta(days=1)
        with self.assertRaises(ImmutableAttestation):
            self.attestation.save()

    def test_queryset_update_raises_for_immutable_fields(self):
        """Attestation.objects.update blocks immutable field bulk writes."""
        with self.assertRaises(ImmutableAttestation):
            Attestation.objects.filter(pk=self.attestation.pk).update(
                payload_hash="0" * 64,
            )

    def test_related_manager_update_raises_for_immutable_fields(self):
        """project.attestations.update blocks immutable field bulk writes."""
        with self.assertRaises(ImmutableAttestation):
            self.project.attestations.update(payload_hash="0" * 64)

    def test_queryset_update_allows_mutable_fields_and_returns_row_count(self):
        """Manager updates keep their integer result while changing mutable markers."""
        disputed_at = timezone.now()

        updated_count = Attestation.objects.filter(pk=self.attestation.pk).update(
            is_disputed=True,
            disputed_at=disputed_at,
        )

        self.attestation.refresh_from_db()
        self.assertEqual(updated_count, 1)
        self.assertTrue(self.attestation.is_disputed)
        self.assertEqual(self.attestation.disputed_at, disputed_at)

    def test_queryset_mixed_update_raises_and_persists_nothing(self):
        """An immutable manager update blocks mutable fields before any write."""
        original_payload_hash = self.attestation.payload_hash

        with self.assertRaises(ImmutableAttestation):
            Attestation.objects.filter(pk=self.attestation.pk).update(
                payload_hash="0" * 64,
                is_disputed=True,
            )

        self.attestation.refresh_from_db()
        self.assertEqual(self.attestation.payload_hash, original_payload_hash)
        self.assertFalse(self.attestation.is_disputed)

    def test_related_manager_update_allows_mutable_fields_and_returns_row_count(self):
        """Related updates match manager success effects and integer return values."""
        disputed_at = timezone.now()

        updated_count = self.project.attestations.update(
            is_disputed=True,
            disputed_at=disputed_at,
        )

        self.attestation.refresh_from_db()
        self.assertEqual(updated_count, 1)
        self.assertTrue(self.attestation.is_disputed)
        self.assertEqual(self.attestation.disputed_at, disputed_at)

    def test_related_manager_mixed_update_raises_and_persists_nothing(self):
        """An immutable related update blocks mutable fields before any write."""
        original_payload_hash = self.attestation.payload_hash

        with self.assertRaises(ImmutableAttestation):
            self.project.attestations.update(
                payload_hash="0" * 64,
                is_disputed=True,
            )

        self.attestation.refresh_from_db()
        self.assertEqual(self.attestation.payload_hash, original_payload_hash)
        self.assertFalse(self.attestation.is_disputed)

    def test_save_update_fields_persists_both_dispute_markers(self):
        """Marker-only update_fields saves remain valid for dispute services and signals."""
        disputed_at = timezone.now()
        self.attestation.is_disputed = True
        self.attestation.disputed_at = disputed_at

        self.attestation.save(update_fields=("is_disputed", "disputed_at"))

        self.attestation.refresh_from_db()
        self.assertTrue(self.attestation.is_disputed)
        self.assertEqual(self.attestation.disputed_at, disputed_at)

    def test_instance_delete_raises_and_preserves_row(self):
        """Instance deletion cannot remove a signed attestation."""
        with self.assertRaises(ImmutableAttestation):
            self.attestation.delete()
        self.assertTrue(Attestation.objects.filter(pk=self.attestation.pk).exists())

    def test_queryset_delete_raises_and_preserves_row(self):
        """Manager queryset deletion cannot remove signed attestations."""
        with self.assertRaises(ImmutableAttestation):
            Attestation.objects.all().delete()
        self.assertTrue(Attestation.objects.filter(pk=self.attestation.pk).exists())

    def test_filtered_queryset_delete_raises_and_preserves_row(self):
        """A realistic filtered bulk deletion keeps its matching signed row."""
        with self.assertRaises(ImmutableAttestation):
            Attestation.objects.filter(pk=self.attestation.pk).delete()
        self.assertTrue(Attestation.objects.filter(pk=self.attestation.pk).exists())

    def test_related_manager_delete_raises_and_preserves_row(self):
        """Related-manager deletion cannot remove signed attestations."""
        with self.assertRaises(ImmutableAttestation):
            self.project.attestations.all().delete()
        self.assertTrue(Attestation.objects.filter(pk=self.attestation.pk).exists())

    def test_flag_dispute_mutates_is_disputed_via_service(self):
        """Dispute markers are mutable through the flag_dispute service."""
        flag_dispute(self.project)
        self.attestation.refresh_from_db()
        self.assertTrue(self.attestation.is_disputed)
        self.assertIsNotNone(self.attestation.disputed_at)


class AmendmentTests(TestCase):
    """Append-only amendments and current-row uniqueness."""

    def setUp(self):
        """Sign a project to obtain an amendable attestation."""
        self.project = make_project_with_items(status=Project.Status.DELIVERED)
        mark_all_items_passed(self.project)
        self.original = sign_attestation(
            self.project,
            "signer@acme.com",
            "Original Signer",
            safe_signature_meta(),
        )

    def test_amend_creates_second_row_and_flips_current_flags(self):
        """Amendment supersedes original while linking amended_from."""
        amendment = amend_attestation(
            self.original,
            "amended@acme.com",
            "Amended Signer",
            safe_signature_meta(),
        )
        self.original.refresh_from_db()
        self.assertFalse(self.original.is_current)
        self.assertTrue(amendment.is_current)
        self.assertEqual(amendment.amended_from_id, self.original.pk)
        self.assertEqual(self.project.attestations.count(), 2)

    def test_amending_non_current_row_raises_immutable_attestation(self):
        """Only the current attestation may be amended."""
        amendment = amend_attestation(
            self.original,
            "amended@acme.com",
            "Amended Signer",
            safe_signature_meta(),
        )
        with self.assertRaises(ImmutableAttestation):
            amend_attestation(
                self.original,
                "again@acme.com",
                "Again",
                safe_signature_meta(),
            )
        self.assertTrue(amendment.is_current)

    def test_amending_disputed_project_raises_invalid_transition(self):
        """Disputed projects cannot receive amendments."""
        flag_dispute(self.project)
        with self.assertRaises(InvalidTransition):
            amend_attestation(
                self.original,
                "amended@acme.com",
                "Amended Signer",
                safe_signature_meta(),
            )

    def test_duplicate_current_attestation_raises_integrity_error(self):
        """Conditional unique constraint allows only one is_current row."""
        payload = canonical_payload(self.project)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Attestation.objects.create(
                    project=self.project,
                    payload=payload,
                    payload_hash=compute_payload_hash(payload),
                    client_email="dup@acme.com",
                    client_name_typed="Duplicate",
                    is_current=True,
                )

    def test_amendment_snapshots_current_normalized_skills(self):
        """An amendment signs current skills without rewriting the original."""
        original_skills = self.original.payload["skills"]
        self.project.skills_csv = "Fast API, Django, fast-api"
        self.project.save(update_fields=("skills_csv", "updated_at"))

        amendment = amend_attestation(
            self.original,
            "amended@acme.com",
            "Amended Signer",
            safe_signature_meta(),
        )

        self.assertEqual(original_skills, [])
        self.assertEqual(amendment.payload["skills"], ["django", "fast-api"])

    def test_amendment_reflects_item_state_change_while_original_stays_immutable(
        self,
    ):
        """Post-sign withdrawal updates only the amendment snapshot."""
        original_payload = dict(self.original.payload)
        original_hash = self.original.payload_hash
        item = self.project.acceptance_items.order_by("order").first()
        withdraw_acceptance_item(item)

        amendment = amend_attestation(
            self.original,
            "amended@acme.com",
            "Amended Signer",
            safe_signature_meta(),
        )
        self.original.refresh_from_db()

        self.assertEqual(self.original.payload, original_payload)
        self.assertEqual(self.original.payload_hash, original_hash)
        self.assertTrue(verify_payload_hash(self.original))
        self.assertFalse(self.original.is_current)
        self.assertTrue(amendment.is_current)
        self.assertEqual(amendment.amended_from_id, self.original.pk)

        amended_items = {
            row["text"]: row
            for row in amendment.payload["acceptance_items"]
        }
        self.assertEqual(
            amended_items["Criterion one"]["state"],
            AcceptanceItem.State.WITHDRAWN,
        )
        self.assertEqual(
            amended_items["Criterion two"]["state"],
            AcceptanceItem.State.APPROVED,
        )

    def test_amendment_reflects_suspended_item_after_signing(self):
        """Post-sign suspension appears only on the amendment snapshot."""
        original_payload = dict(self.original.payload)
        item = self.project.acceptance_items.order_by("order").first()
        suspend_acceptance_item(item)

        amendment = amend_attestation(
            self.original,
            "amended@acme.com",
            "Amended Signer",
            safe_signature_meta(),
        )
        self.original.refresh_from_db()

        self.assertEqual(self.original.payload, original_payload)
        self.assertTrue(verify_payload_hash(self.original))
        amended_items = {
            row["text"]: row
            for row in amendment.payload["acceptance_items"]
        }
        self.assertEqual(
            amended_items["Criterion one"]["state"],
            AcceptanceItem.State.SUSPENDED,
        )


class PublicDisplayTests(TestCase):
    """Public record queries respect dispute freeze and current-row rules."""

    def setUp(self):
        """Create a profile with one signed, current, clean attestation."""
        self.profile = make_profile()
        self.project = make_project_with_items(
            status=Project.Status.DELIVERED,
            owner=self.profile,
        )
        mark_all_items_passed(self.project)
        self.attestation = sign_attestation(
            self.project,
            "signer@acme.com",
            "Signer",
            safe_signature_meta(),
        )

    def test_public_attestations_includes_clean_current_row(self):
        """Non-disputed current attestation appears in the public record."""
        public = list(public_attestations(self.profile))
        self.assertEqual(len(public), 1)
        self.assertEqual(public[0].pk, self.attestation.pk)

    def test_public_attestations_excludes_disputed_row(self):
        """Disputed attestations are hidden from the public record."""
        flag_dispute(self.project)
        self.assertEqual(len(public_attestations(self.profile)), 0)

    def test_public_attestations_excludes_non_current_row(self):
        """Superseded amendments do not appear in the public record."""
        amend_attestation(
            self.attestation,
            "amended@acme.com",
            "Amended",
            safe_signature_meta(),
        )
        public_ids = {
            attestation.pk for attestation in public_attestations(self.profile)
        }
        self.assertNotIn(self.attestation.pk, public_ids)
        self.assertEqual(len(public_ids), 1)

    def test_is_disputed_filter_excludes_independently_of_project_status(self):
        """is_disputed=True alone hides the row while project stays attested."""
        self.attestation.is_disputed = True
        self.attestation.save(update_fields=("is_disputed",))
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.ATTESTED)
        self.assertEqual(len(public_attestations(self.profile)), 0)

    def test_project_status_filter_excludes_independently_of_is_disputed(self):
        """Disputed project status alone hides a clean attestation row."""
        Project.objects.filter(pk=self.project.pk).update(
            status=Project.Status.DISPUTED,
        )
        self.attestation.refresh_from_db()
        self.assertFalse(self.attestation.is_disputed)
        self.assertEqual(len(public_attestations(self.profile)), 0)

    def test_disputed_count_reflects_disputed_current_rows(self):
        """disputed_count tracks current rows flagged as disputed."""
        self.assertEqual(disputed_count(self.profile), 0)
        flag_dispute(self.project)
        self.assertEqual(disputed_count(self.profile), 1)

    def test_i1a_payload_hidden_when_is_disputed_on_attested_project(self):
        """is_disputed alone freezes I1a-shaped payloads on attested projects."""
        payload_items = self.attestation.payload["acceptance_items"]
        self.assertTrue(all("state" in item for item in payload_items))

        self.attestation.is_disputed = True
        self.attestation.disputed_at = timezone.now()
        self.attestation.save(update_fields=("is_disputed", "disputed_at"))
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.ATTESTED)

        self.assertEqual(len(public_attestations(self.profile)), 0)
        self.assertEqual(disputed_count(self.profile), 1)

    def test_dispute_freeze_and_restore_on_public_record(self):
        """Attestation disappears on dispute and returns after resolve."""
        self.assertEqual(len(public_attestations(self.profile)), 1)
        flag_dispute(self.project)
        self.assertEqual(len(public_attestations(self.profile)), 0)
        resolve_dispute(self.project)
        self.assertEqual(len(public_attestations(self.profile)), 1)

    def test_tampered_payload_fails_hash_and_is_excluded_beside_clean_sibling(self):
        """Raw database tampering cannot render as a verified public record."""
        sibling_project = make_project_with_items(
            status=Project.Status.DELIVERED,
            owner=self.profile,
        )
        sibling = sign_project(sibling_project)
        tampered_payload = dict(self.attestation.payload)
        tampered_payload["brief"] = "Database-tampered brief"

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE ledger_attestation SET payload = %s WHERE id = %s",
                [json.dumps(tampered_payload), self.attestation.pk],
            )
        self.attestation.refresh_from_db()

        self.assertFalse(verify_payload_hash(self.attestation))
        self.assertTrue(verify_payload_hash(sibling))
        self.assertEqual(
            [attestation.pk for attestation in public_attestations(self.profile)],
            [sibling.pk],
        )


class CapabilityTagTests(TestCase):
    """Capability tags derive from clean, attested projects."""

    def setUp(self):
        """Profile and project with two skills ready to sign."""
        self.profile = make_profile()
        self.project = make_project_with_items(
            status=Project.Status.DELIVERED,
            skills_csv="django, htmx",
            owner=self.profile,
        )
        mark_all_items_passed(self.project)

    def test_sign_creates_tags_from_skills_csv(self):
        """Signing recomputes two capability tags with attested_count 1."""
        sign_attestation(
            self.project,
            "signer@acme.com",
            "Signer",
            safe_signature_meta(),
        )
        tags = CapabilityTag.objects.filter(profile=self.profile).order_by("name")
        self.assertEqual(tags.count(), 2)
        self.assertEqual(list(tags.values_list("name", "attested_count")), [
            ("django", 1),
            ("htmx", 1),
        ])

    def test_recompute_uses_signed_skills_after_live_project_skills_change(self):
        """Capability tags remain tied to immutable signed skill data."""
        attestation = sign_attestation(
            self.project,
            "signer@acme.com",
            "Signer",
            safe_signature_meta(),
        )
        self.assertEqual(attestation.payload["skills"], ["django", "htmx"])
        self.project.skills_csv = "python"
        self.project.save(update_fields=("skills_csv", "updated_at"))

        recompute_capability_tags(self.profile)

        self.assertEqual(
            list(
                CapabilityTag.objects.filter(profile=self.profile).values_list(
                    "name",
                    flat=True,
                )
            ),
            ["django", "htmx"],
        )

    def test_pre_i1a_payload_still_verifies_and_contributes_tags(self):
        """An old signed shape remains valid and derives its stored skills."""
        legacy_project = Project.objects.create(
            owner=self.profile,
            title="Legacy signed project",
            client_name="Legacy Client",
            client_email="legacy@example.com",
            brief="Signed before per-item state",
            skills_csv="changed-after-signing",
            status=Project.Status.ATTESTED,
        )
        legacy_payload = {
            "acceptance_items": [
                {
                    "text": "Legacy criterion",
                    "is_passed": True,
                    "evidence_url": "",
                }
            ],
            "approved_change_orders": [],
            "brief": "Signed before per-item state",
            "revision_limit": 2,
            "skills": ["django", "legacy-skill"],
            "title": "Legacy signed project",
        }
        attestation = Attestation.objects.create(
            project=legacy_project,
            payload=legacy_payload,
            payload_hash=compute_payload_hash(legacy_payload),
            client_email="legacy@example.com",
            client_name_typed="Legacy Signer",
            signature_meta=safe_signature_meta(),
        )

        self.assertTrue(verify_payload_hash(attestation))
        self.assertNotIn(
            "state",
            attestation.payload["acceptance_items"][0],
        )
        self.assertNotIn(
            "steps",
            attestation.payload["acceptance_items"][0],
        )
        recompute_capability_tags(self.profile)
        self.assertEqual(
            list(
                CapabilityTag.objects.filter(profile=self.profile)
                .order_by("name")
                .values_list("name", flat=True)
            ),
            ["django", "legacy-skill"],
        )
        attestation.refresh_from_db()
        self.assertEqual(attestation.payload, legacy_payload)
        self.assertTrue(verify_payload_hash(attestation))
        public = list(public_attestations(self.profile))
        self.assertEqual(len(public), 1)
        self.assertEqual(public[0].pk, attestation.pk)

    def test_legacy_and_i1a_attestations_combine_capability_tags(self):
        """Pre-I1a and new-shaped attestations both contribute capability tags."""
        legacy_project = Project.objects.create(
            owner=self.profile,
            title="Legacy signed project",
            client_name="Legacy Client",
            client_email="legacy@example.com",
            brief="Signed before per-item state",
            skills_csv="changed-after-signing",
            status=Project.Status.ATTESTED,
        )
        legacy_payload = {
            "acceptance_items": [
                {
                    "text": "Legacy criterion",
                    "is_passed": True,
                    "evidence_url": "",
                }
            ],
            "approved_change_orders": [],
            "brief": "Signed before per-item state",
            "revision_limit": 2,
            "skills": ["django", "legacy-skill"],
            "title": "Legacy signed project",
        }
        Attestation.objects.create(
            project=legacy_project,
            payload=legacy_payload,
            payload_hash=compute_payload_hash(legacy_payload),
            client_email="legacy@example.com",
            client_name_typed="Legacy Signer",
            signature_meta=safe_signature_meta(),
        )

        sign_attestation(
            self.project,
            "signer@acme.com",
            "Signer",
            safe_signature_meta(),
        )

        tags = CapabilityTag.objects.filter(profile=self.profile).order_by("name")
        self.assertEqual(
            list(tags.values_list("name", "attested_count")),
            [
                ("django", 2),
                ("htmx", 1),
                ("legacy-skill", 1),
            ],
        )

    def test_dispute_removes_capability_tags(self):
        """Disputing an attested project clears derived capability tags."""
        sign_attestation(
            self.project,
            "signer@acme.com",
            "Signer",
            safe_signature_meta(),
        )
        self.assertEqual(CapabilityTag.objects.filter(profile=self.profile).count(), 2)
        flag_dispute(self.project)
        self.assertEqual(CapabilityTag.objects.filter(profile=self.profile).count(), 0)

    def test_resolve_dispute_restores_capability_tags(self):
        """Resolving a dispute recomputes capability tags from clean data."""
        sign_attestation(
            self.project,
            "signer@acme.com",
            "Signer",
            safe_signature_meta(),
        )
        flag_dispute(self.project)
        resolve_dispute(self.project)
        tags = CapabilityTag.objects.filter(profile=self.profile).order_by("name")
        self.assertEqual(tags.count(), 2)
        self.assertEqual(list(tags.values_list("name", "attested_count")), [
            ("django", 1),
            ("htmx", 1),
        ])


class DisputeCacheFreshnessTests(TestCase):
    """Every dispute write path keeps the CapabilityTag cache authoritative."""

    def setUp(self):
        """Sign one skill-bearing project and authenticate a superuser."""
        self.profile = make_profile()
        self.project = make_project_with_items(
            status=Project.Status.DELIVERED,
            skills_csv="Django REST",
            owner=self.profile,
        )
        mark_all_items_passed(self.project)
        self.attestation = sign_attestation(
            self.project,
            "signer@acme.com",
            "Signer",
            safe_signature_meta(),
        )
        self.assertEqual(self.attestation.payload["skills"], ["django-rest"])
        self.admin_user = User.objects.create_superuser(
            username="ledger-admin",
            email="admin@example.com",
            password="testpass",
        )
        self.client = Client()
        self.client.force_login(self.admin_user)

    def assert_cache_rows(self, profile, expected_rows):
        """Assert exact cached names, counts, and signed timestamps."""
        actual_rows = list(
            CapabilityTag.objects.filter(profile=profile)
            .order_by("name")
            .values_list("name", "attested_count", "last_attested_at")
        )
        self.assertEqual(actual_rows, expected_rows)

    def run_admin_action(self, action, attestations):
        """Post one attestation admin action and return its followed response."""
        return self.client.post(
            reverse("admin:ledger_attestation_changelist"),
            {
                "action": action,
                "_selected_action": [
                    attestation.pk for attestation in attestations
                ],
                "index": "0",
            },
            follow=True,
        )

    def test_service_dispute_round_trip_refreshes_exact_cache_rows(self):
        """Service flag removes and resolve restores the exact signed cache row."""
        expected_rows = [
            ("django-rest", 1, self.attestation.signed_at),
        ]
        self.assert_cache_rows(self.profile, expected_rows)

        flag_dispute(self.project)
        self.assert_cache_rows(self.profile, [])

        resolve_dispute(self.project)
        self.assert_cache_rows(self.profile, expected_rows)

    def test_direct_save_dispute_round_trip_refreshes_exact_cache_rows(self):
        """The post-save layer refreshes both directions of a direct marker write."""
        expected_rows = [
            ("django-rest", 1, self.attestation.signed_at),
        ]
        self.attestation.is_disputed = True
        self.attestation.disputed_at = timezone.now()
        self.attestation.save(
            update_fields=("is_disputed", "disputed_at"),
        )
        self.assert_cache_rows(self.profile, [])

        self.attestation.is_disputed = False
        self.attestation.disputed_at = None
        self.attestation.save(
            update_fields=("is_disputed", "disputed_at"),
        )
        self.assert_cache_rows(self.profile, expected_rows)

    def test_direct_is_current_save_unconditionally_refreshes_cache(self):
        """A non-dispute update still triggers the unconditional cache receiver."""
        expected_rows = [
            ("django-rest", 1, self.attestation.signed_at),
        ]
        self.attestation.is_current = False
        self.attestation.save(update_fields=("is_current",))
        self.assert_cache_rows(self.profile, [])

        self.attestation.is_current = True
        self.attestation.save(update_fields=("is_current",))
        self.assert_cache_rows(self.profile, expected_rows)

    def test_queryset_update_dispute_round_trip_refreshes_exact_cache_rows(self):
        """The queryset wrapper refreshes both directions of bulk marker writes."""
        expected_rows = [
            ("django-rest", 1, self.attestation.signed_at),
        ]
        Attestation.objects.filter(pk=self.attestation.pk).update(
            is_disputed=True,
            disputed_at=timezone.now(),
        )
        self.assert_cache_rows(self.profile, [])

        Attestation.objects.filter(pk=self.attestation.pk).update(
            is_disputed=False,
            disputed_at=None,
        )
        self.assert_cache_rows(self.profile, expected_rows)

    def test_related_manager_update_refreshes_exact_cache_rows(self):
        """The related-manager bulk path cannot bypass the queryset cache wrapper."""
        expected_rows = [
            ("django-rest", 1, self.attestation.signed_at),
        ]
        self.project.attestations.update(
            is_disputed=True,
            disputed_at=timezone.now(),
        )
        self.assert_cache_rows(self.profile, [])

        self.project.attestations.update(
            is_disputed=False,
            disputed_at=None,
        )
        self.assert_cache_rows(self.profile, expected_rows)

    def test_admin_action_dispute_round_trip_refreshes_exact_cache_rows(self):
        """Admin actions preserve service transitions and exact cache contents."""
        expected_rows = [
            ("django-rest", 1, self.attestation.signed_at),
        ]
        with (
            patch(
                "ledger.admin.flag_dispute",
                wraps=flag_dispute,
            ) as flag_service,
            patch(
                "ledger.admin.resolve_dispute",
                wraps=resolve_dispute,
            ) as unused_resolve_service,
        ):
            flag_response = self.run_admin_action(
                "flag_selected_disputes",
                [self.attestation],
            )
        flag_service.assert_called_once()
        unused_resolve_service.assert_not_called()
        flagged_project = flag_service.call_args.args[0]
        self.assertEqual(flagged_project.pk, self.project.pk)
        self.assertEqual(flag_response.status_code, 200)
        self.assertContains(
            flag_response,
            "1 project(s) flagged as disputed; "
            "0 duplicate attestation(s) skipped; "
            "0 invalid transition(s) skipped.",
        )
        self.assert_cache_rows(self.profile, [])

        with (
            patch(
                "ledger.admin.resolve_dispute",
                wraps=resolve_dispute,
            ) as resolve_service,
            patch(
                "ledger.admin.flag_dispute",
                wraps=flag_dispute,
            ) as unused_flag_service,
        ):
            resolve_response = self.run_admin_action(
                "resolve_selected_disputes",
                [self.attestation],
            )
        resolve_service.assert_called_once()
        unused_flag_service.assert_not_called()
        resolved_project = resolve_service.call_args.args[0]
        self.assertEqual(resolved_project.pk, self.project.pk)
        self.assertEqual(resolve_response.status_code, 200)
        self.assertContains(
            resolve_response,
            "1 project(s) resolved; "
            "0 duplicate attestation(s) skipped; "
            "0 invalid transition(s) skipped.",
        )
        self.assert_cache_rows(self.profile, expected_rows)

    def test_attestation_admin_hostile_marker_post_succeeds_without_changes(self):
        """Read-only dispute fields ignore a hostile POST that otherwise succeeds."""
        attempted_disputed_at = timezone.now() + timedelta(days=1)
        response = self.client.post(
            reverse(
                "admin:ledger_attestation_change",
                args=[self.attestation.pk],
            ),
            {
                "is_disputed": "on",
                "disputed_at_0": attempted_disputed_at.date().isoformat(),
                "disputed_at_1": attempted_disputed_at.time().isoformat(),
            },
        )

        self.assertRedirects(
            response,
            reverse("admin:ledger_attestation_changelist"),
        )
        self.attestation.refresh_from_db()
        self.assertFalse(self.attestation.is_disputed)
        self.assertIsNone(self.attestation.disputed_at)

    def test_attestation_admin_renders_dispute_markers_read_only(self):
        """The change form renders marker rows without editable marker inputs."""
        response = self.client.get(
            reverse(
                "admin:ledger_attestation_change",
                args=[self.attestation.pk],
            ),
        )

        self.assertEqual(response.status_code, 200)
        rendered_form = response.content.decode()
        # Forbid an intervening form-row so a later readonly field cannot
        # satisfy the match on behalf of the marker row being asserted.
        self.assertRegex(
            rendered_form,
            r'(?s)class="form-row field-is_disputed"'
            r'(?:(?!form-row).)*?class="readonly"',
        )
        self.assertRegex(
            rendered_form,
            r'(?s)class="form-row field-disputed_at"'
            r'(?:(?!form-row).)*?class="readonly"',
        )
        self.assertNotContains(response, 'name="is_disputed"')
        self.assertNotContains(response, 'name="disputed_at_0"')
        self.assertNotContains(response, 'name="disputed_at_1"')

    def test_post_save_cache_receiver_is_connected_to_attestation(self):
        """Pin AppConfig.ready wiring to Attestation and no unrelated sender."""
        synchronous_receivers, asynchronous_receivers = post_save._live_receivers(
            Attestation
        )
        unrelated_receivers, unrelated_async_receivers = post_save._live_receivers(
            Profile
        )

        self.assertIn(
            recompute_capability_tags_after_attestation_save,
            synchronous_receivers,
        )
        self.assertNotIn(
            recompute_capability_tags_after_attestation_save,
            unrelated_receivers,
        )
        self.assertEqual(asynchronous_receivers, [])
        self.assertEqual(unrelated_async_receivers, [])

    def test_queryset_project_move_refreshes_old_and_new_owner_caches(self):
        """Owner-union refresh prevents stale tags after a bulk project reassignment."""
        new_owner = make_profile()
        target_project = make_project_with_items(
            status=Project.Status.ATTESTED,
            owner=new_owner,
        )
        existing_project = make_project_with_items(
            status=Project.Status.DELIVERED,
            skills_csv="Python",
            owner=new_owner,
        )
        mark_all_items_passed(existing_project)
        existing_attestation = sign_attestation(
            existing_project,
            "other-signer@acme.com",
            "Other Signer",
            safe_signature_meta(),
        )
        self.assert_cache_rows(
            self.profile,
            [("django-rest", 1, self.attestation.signed_at)],
        )
        self.assert_cache_rows(
            new_owner,
            [("python", 1, existing_attestation.signed_at)],
        )

        Attestation.objects.filter(project=self.project).update(
            project=target_project,
        )

        self.assert_cache_rows(self.profile, [])
        self.assert_cache_rows(
            new_owner,
            [
                ("django-rest", 1, self.attestation.signed_at),
                ("python", 1, existing_attestation.signed_at),
            ],
        )

    def test_admin_action_deduplicates_attestations_from_one_project(self):
        """Two selected amendments cause one transition and report one duplicate."""
        amendment = amend_attestation(
            self.attestation,
            "amended@acme.com",
            "Amended Signer",
            safe_signature_meta(),
        )

        response = self.run_admin_action(
            "flag_selected_disputes",
            [self.attestation, amendment],
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "1 project(s) flagged as disputed; "
            "1 duplicate attestation(s) skipped; "
            "0 invalid transition(s) skipped.",
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.DISPUTED)
        self.assert_cache_rows(self.profile, [])

    def test_admin_action_skips_invalid_project_and_processes_valid_peer(self):
        """An invalid transition is reported without blocking a valid selection."""
        invalid_project = make_project_with_items(
            status=Project.Status.DELIVERED,
            skills_csv="Python",
        )
        mark_all_items_passed(invalid_project)
        invalid_attestation = sign_attestation(
            invalid_project,
            "invalid@acme.com",
            "Invalid State Signer",
            safe_signature_meta(),
        )
        Project.objects.filter(pk=invalid_project.pk).update(
            status=Project.Status.DRAFT,
        )

        response = self.run_admin_action(
            "flag_selected_disputes",
            [invalid_attestation, self.attestation],
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "1 project(s) flagged as disputed; "
            "0 duplicate attestation(s) skipped; "
            "1 invalid transition(s) skipped.",
        )
        self.project.refresh_from_db()
        invalid_project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.DISPUTED)
        self.assertEqual(invalid_project.status, Project.Status.DRAFT)
        self.assert_cache_rows(self.profile, [])


class ModelConstraintTests(TestCase):
    """Database-level integrity constraints on ledger models."""

    def test_change_order_negative_amount_raises_integrity_error(self):
        """amount_cents must be non-negative."""
        project = make_project_with_items()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ChangeOrder.objects.create(
                    project=project,
                    description="Bad discount",
                    amount_cents=-100,
                    timeline_days=0,
                )

    def test_change_order_negative_timeline_raises_integrity_error(self):
        """timeline_days must be non-negative."""
        project = make_project_with_items()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ChangeOrder.objects.create(
                    project=project,
                    description="Bad timeline",
                    amount_cents=0,
                    timeline_days=-5,
                )

    def test_duplicate_acceptance_item_order_raises_integrity_error(self):
        """Each project may have only one criterion per order position."""
        project = make_project_with_items()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AcceptanceItem.objects.create(
                    project=project,
                    text="Duplicate order slot",
                    order=1,
                )

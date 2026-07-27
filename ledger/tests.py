"""Verifier tests for the Ledger core (M1)."""

import json
from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone

from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from ledger.models import (
    AcceptanceItem,
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
    amend_attestation,
    approve_acceptance_item,
    approve_acceptance_items,
    approve_change_order,
    approve_criteria,
    canonical_payload,
    compute_payload_hash,
    criteria_locked,
    decline_change_order,
    delete_acceptance_item,
    disputed_count,
    flag_dispute,
    has_open_change_orders,
    mark_delivered,
    propose_change_order,
    public_attestations,
    pull_back_acceptance_item,
    recompute_capability_tags,
    reopen_active,
    resolve_dispute,
    resume_acceptance_item,
    set_profile_visibility,
    sign_attestation,
    submit_acceptance_item_for_approval,
    submit_acceptance_items_for_approval,
    submit_criteria_for_approval,
    suspend_acceptance_item,
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

        profile = make_profile()
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

    def test_delete_allows_never_approved_and_rejects_approved_history(self):
        """Hard delete follows approved_at even while an item is suspended."""
        self.set_state(
            AcceptanceItem.State.SUSPENDED,
            submitted=True,
            approved=False,
        )
        deletable_pk = self.item.pk
        delete_acceptance_item(self.item)
        self.assertFalse(
            AcceptanceItem.objects.filter(pk=deletable_pk).exists()
        )

        approved_item = self.project.acceptance_items.order_by("order").last()
        approved_item.state = AcceptanceItem.State.SUSPENDED
        approved_item.submitted_at = timezone.now()
        approved_item.approved_at = timezone.now()
        approved_item.save(
            update_fields=("state", "submitted_at", "approved_at"),
        )
        with self.assertRaises(InvalidTransition):
            delete_acceptance_item(approved_item)
        self.assertTrue(
            AcceptanceItem.objects.filter(pk=approved_item.pk).exists()
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
        AcceptanceItem.objects.create(
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

        self.assertEqual(
            compute_payload_hash(canonical_payload(project)),
            "302fc585ff14c2b1fda0c67375100b4ec8d802368bbf92306815cd56ef53dc19",
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
            },
        )
        self.assertEqual(payload_items[1]["state"], "suspended")
        self.assertEqual(
            payload_items[1]["submitted_at"],
            "2026-02-03T04:05:06+00:00",
        )
        self.assertIsNone(payload_items[1]["approved_at"])

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

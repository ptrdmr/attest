"""Verifier tests for the Ledger core (M1)."""

import hashlib
import json

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase

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
    amend_attestation,
    approve_criteria,
    canonical_payload,
    compute_payload_hash,
    criteria_locked,
    disputed_count,
    flag_dispute,
    mark_delivered,
    public_attestations,
    reopen_active,
    resolve_dispute,
    sign_attestation,
    submit_criteria_for_approval,
)


def safe_signature_meta():
    """Return signature metadata that passes service validation."""
    return {"user_agent": "TestAgent/1.0", "ip_hash": "abc123def456"}


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
    AcceptanceItem.objects.create(
        project=project,
        text="Criterion one",
        order=1,
        is_passed=None,
    )
    AcceptanceItem.objects.create(
        project=project,
        text="Criterion two",
        order=2,
        is_passed=None,
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
    """Mark every acceptance item on the project as passed."""
    project.acceptance_items.update(is_passed=True)


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
        """Hash equals SHA-256 of the key-sorted, compact JSON string."""
        payload = {
            "title": "Golden Project",
            "brief": "Fixed brief",
            "revision_limit": 2,
            "acceptance_items": [
                {"text": "A", "is_passed": True, "evidence_url": ""},
            ],
            "approved_change_orders": [],
        }
        canonical_json = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        expected = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        self.assertEqual(compute_payload_hash(payload), expected)

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

    def test_raises_when_any_acceptance_item_is_unverified(self):
        """is_passed=None on any item blocks signing."""
        project = make_project_with_items(status=Project.Status.DELIVERED)
        items = list(project.acceptance_items.order_by("order"))
        items[0].is_passed = True
        items[0].save(update_fields=("is_passed",))
        with self.assertRaises(InvalidTransition):
            sign_attestation(
                project,
                "signer@acme.com",
                "Signer",
                safe_signature_meta(),
            )

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
        from django.utils import timezone

        self.attestation.signed_at = timezone.now()
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
        self.assertEqual(public_attestations(self.profile).count(), 0)

    def test_public_attestations_excludes_non_current_row(self):
        """Superseded amendments do not appear in the public record."""
        amend_attestation(
            self.attestation,
            "amended@acme.com",
            "Amended",
            safe_signature_meta(),
        )
        public_ids = set(
            public_attestations(self.profile).values_list("pk", flat=True)
        )
        self.assertNotIn(self.attestation.pk, public_ids)
        self.assertEqual(len(public_ids), 1)

    def test_is_disputed_filter_excludes_independently_of_project_status(self):
        """is_disputed=True alone hides the row while project stays attested."""
        self.attestation.is_disputed = True
        self.attestation.save(update_fields=("is_disputed",))
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.ATTESTED)
        self.assertEqual(public_attestations(self.profile).count(), 0)

    def test_project_status_filter_excludes_independently_of_is_disputed(self):
        """Disputed project status alone hides a clean attestation row."""
        Project.objects.filter(pk=self.project.pk).update(
            status=Project.Status.DISPUTED,
        )
        self.attestation.refresh_from_db()
        self.assertFalse(self.attestation.is_disputed)
        self.assertEqual(public_attestations(self.profile).count(), 0)

    def test_disputed_count_reflects_disputed_current_rows(self):
        """disputed_count tracks current rows flagged as disputed."""
        self.assertEqual(disputed_count(self.profile), 0)
        flag_dispute(self.project)
        self.assertEqual(disputed_count(self.profile), 1)

    def test_dispute_freeze_and_restore_on_public_record(self):
        """Attestation disappears on dispute and returns after resolve."""
        self.assertEqual(public_attestations(self.profile).count(), 1)
        flag_dispute(self.project)
        self.assertEqual(public_attestations(self.profile).count(), 0)
        resolve_dispute(self.project)
        self.assertEqual(public_attestations(self.profile).count(), 1)


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

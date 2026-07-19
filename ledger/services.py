import hashlib
import json
from collections import defaultdict

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from .models import (
    Attestation,
    CapabilityTag,
    ChangeOrder,
    ImmutableAttestation,
    Project,
)


class InvalidTransition(Exception):
    """Indicate that a project cannot enter the requested state."""


class InvalidSignatureMeta(Exception):
    """Indicate unsafe or unsupported signature metadata."""


def _transition(project, expected_status, target_status):
    """Move a project between two explicitly permitted workflow states."""
    if project.status != expected_status:
        raise InvalidTransition(
            f"Cannot transition from {project.status} to {target_status}."
        )
    project.status = target_status
    project.save(update_fields=("status", "updated_at"))
    return project


def submit_criteria_for_approval(project):
    """Move a draft project to client criteria review."""
    return _transition(
        project,
        Project.Status.DRAFT,
        Project.Status.CRITERIA_PENDING,
    )


def approve_criteria(project):
    """Activate a project whose criteria have been approved."""
    return _transition(
        project,
        Project.Status.CRITERIA_PENDING,
        Project.Status.ACTIVE,
    )


def mark_delivered(project):
    """Mark an active project as delivered and ready to attest."""
    return _transition(
        project,
        Project.Status.ACTIVE,
        Project.Status.DELIVERED,
    )


def reopen_active(project):
    """Return a delivered project to active work."""
    return _transition(
        project,
        Project.Status.DELIVERED,
        Project.Status.ACTIVE,
    )


def criteria_locked(project):
    """Return whether acceptance criteria are no longer editable."""
    return project.status not in {
        Project.Status.DRAFT,
        Project.Status.CRITERIA_PENDING,
    }


def canonical_payload(project):
    """Build the deterministic signed snapshot for a project."""
    acceptance_items = list(
        project.acceptance_items.order_by("order", "pk").values(
            "text",
            "is_passed",
            "evidence_url",
        )
    )
    change_orders = list(
        project.change_orders.filter(status=ChangeOrder.Status.APPROVED)
        .order_by("pk")
        .values("description", "amount_cents")
    )
    return {
        "acceptance_items": acceptance_items,
        "approved_change_orders": change_orders,
        "brief": project.brief,
        "revision_limit": project.revision_limit,
        "title": project.title,
    }


def compute_payload_hash(payload: dict) -> str:
    """Return the SHA-256 hex digest of canonical JSON payload bytes."""
    canonical_json = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _safe_signature_meta(signature_meta):
    """Validate metadata so a raw client IP cannot be persisted."""
    allowed_keys = {"user_agent", "ip_hash", "ip_truncated"}
    unexpected_keys = set(signature_meta) - allowed_keys
    if unexpected_keys:
        raise InvalidSignatureMeta(
            "Signature metadata contains unsupported fields."
        )
    return dict(signature_meta)


@transaction.atomic
def sign_attestation(
    project,
    client_email,
    client_name_typed,
    signature_meta,
):
    """Sign a delivered project snapshot and move it to attested."""
    if project.status != Project.Status.DELIVERED:
        raise InvalidTransition("Only delivered projects can be attested.")
    if project.acceptance_items.filter(is_passed__isnull=True).exists():
        raise InvalidTransition("All acceptance items must be verified.")

    payload = canonical_payload(project)
    attestation = Attestation.objects.create(
        project=project,
        payload=payload,
        payload_hash=compute_payload_hash(payload),
        client_email=client_email,
        client_name_typed=client_name_typed,
        signed_at=timezone.now(),
        signature_meta=_safe_signature_meta(signature_meta),
    )
    _transition(project, Project.Status.DELIVERED, Project.Status.ATTESTED)
    recompute_capability_tags(project.owner)
    return attestation


def _current_attestation(project):
    """Return the single current attestation for an attested project."""
    return project.attestations.get(is_current=True)


@transaction.atomic
def amend_attestation(
    original,
    client_email,
    client_name_typed,
    signature_meta,
):
    """Append a re-signed snapshot that supersedes the original row."""
    if not original.is_current:
        raise ImmutableAttestation(
            "Only the current attestation can be amended."
        )
    project = original.project
    if project.status != Project.Status.ATTESTED:
        raise InvalidTransition("Only attested projects can be amended.")

    original.is_current = False
    original.save(update_fields=("is_current",))
    payload = canonical_payload(project)
    amendment = Attestation.objects.create(
        project=project,
        payload=payload,
        payload_hash=compute_payload_hash(payload),
        client_email=client_email,
        client_name_typed=client_name_typed,
        signed_at=timezone.now(),
        signature_meta=_safe_signature_meta(signature_meta),
        amended_from=original,
        is_current=True,
    )
    recompute_capability_tags(project.owner)
    return amendment


@transaction.atomic
def flag_dispute(project):
    """Flag a signed project and its attestation as disputed."""
    if project.status != Project.Status.ATTESTED:
        raise InvalidTransition("Only attested projects can be disputed.")
    attestation = _current_attestation(project)
    attestation.is_disputed = True
    attestation.disputed_at = timezone.now()
    attestation.save(update_fields=("is_disputed", "disputed_at"))
    _transition(project, Project.Status.ATTESTED, Project.Status.DISPUTED)
    recompute_capability_tags(project.owner)
    return project


@transaction.atomic
def resolve_dispute(project):
    """Clear dispute markers and restore the attested project state."""
    if project.status != Project.Status.DISPUTED:
        raise InvalidTransition("Only disputed projects can be resolved.")
    attestation = _current_attestation(project)
    attestation.is_disputed = False
    attestation.disputed_at = None
    attestation.save(update_fields=("is_disputed", "disputed_at"))
    _transition(project, Project.Status.DISPUTED, Project.Status.ATTESTED)
    recompute_capability_tags(project.owner)
    return project


def _tag_dates_by_name(profile):
    """Collect one signed date per normalized skill and project."""
    tag_dates = defaultdict(list)
    attestations = Attestation.objects.filter(
        project__owner=profile,
        project__status=Project.Status.ATTESTED,
        is_current=True,
        is_disputed=False,
    ).select_related("project")
    for attestation in attestations:
        project_tags = {
            slugify(tag.strip())
            for tag in attestation.project.skills_csv.split(",")
            if slugify(tag.strip())
        }
        for tag_name in project_tags:
            tag_dates[tag_name].append(attestation.signed_at)
    return tag_dates


@transaction.atomic
def recompute_capability_tags(profile):
    """Replace a profile's capability cache from clean attestations."""
    tag_dates = _tag_dates_by_name(profile)
    CapabilityTag.objects.filter(profile=profile).delete()
    tags = [
        CapabilityTag(
            profile=profile,
            name=name,
            attested_count=len(signed_dates),
            last_attested_at=max(signed_dates),
        )
        for name, signed_dates in sorted(tag_dates.items())
    ]
    CapabilityTag.objects.bulk_create(tags)
    return tags


def public_attestations(profile):
    """Return clean signed attestations safe for the public record."""
    return Attestation.objects.filter(
        project__owner=profile,
        project__status=Project.Status.ATTESTED,
        is_current=True,
        is_disputed=False,
    ).select_related("project")


def disputed_count(profile):
    """Return the number of disputed attestations for annotation."""
    return Attestation.objects.filter(
        project__owner=profile,
        is_current=True,
        is_disputed=True,
    ).count()

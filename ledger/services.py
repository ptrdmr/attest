import hashlib
import json
from collections import defaultdict

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from .models import (
    AcceptanceItem,
    AcceptanceStep,
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


def set_profile_visibility(profile, is_public):
    """Set a Profile's visibility from a boolean and return the saved Profile."""
    if not isinstance(is_public, bool):
        raise ValueError("is_public must be a boolean.")
    profile.is_public = is_public
    profile.save(update_fields=("is_public",))
    return profile


_PROFILE_DETAIL_FIELDS = (
    "display_name",
    "headline",
    "bio",
    "location",
    "website_url",
)


def set_profile_details(profile, **fields):
    """Set supplied Profile identity fields and return the saved Profile.

    Raises ValueError for handle, unknown keys, blank display_name, or an empty
    call. Raises ValidationError when a supplied value fails field validation.
    """
    if "handle" in fields:
        raise ValueError("handle cannot be changed.")
    unexpected_keys = set(fields) - set(_PROFILE_DETAIL_FIELDS)
    if unexpected_keys:
        raise ValueError("Profile details contain unsupported fields.")
    if not fields:
        raise ValueError("At least one profile field must be supplied.")

    update_fields = []
    for field_name in _PROFILE_DETAIL_FIELDS:
        if field_name not in fields:
            continue
        value = fields[field_name]
        if field_name == "display_name":
            if not isinstance(value, str) or not value.strip():
                raise ValueError("display_name must not be empty.")
            value = value.strip()
        setattr(profile, field_name, value)
        update_fields.append(field_name)

    exclude = [
        field.name
        for field in profile._meta.concrete_fields
        if field.name not in update_fields
    ]
    profile.full_clean(validate_unique=False, exclude=exclude)
    profile.save(update_fields=tuple(update_fields))
    return profile


def _transition(project, expected_status, target_status):
    """Move a project between two explicitly permitted workflow states."""
    if project.status != expected_status:
        raise InvalidTransition(
            f"Cannot transition from {project.status} to {target_status}."
        )
    project.status = target_status
    project.save(update_fields=("status", "updated_at"))
    return project


@transaction.atomic
def submit_criteria_for_approval(project):
    """Submit all draft items and move a draft project to client review."""
    submitted_at = timezone.now()
    project.acceptance_items.filter(
        state=AcceptanceItem.State.DRAFT,
    ).update(
        state=AcceptanceItem.State.SUBMITTED,
        submitted_at=submitted_at,
    )
    return _transition(
        project,
        Project.Status.DRAFT,
        Project.Status.CRITERIA_PENDING,
    )


@transaction.atomic
def approve_criteria(project):
    """Approve all submitted items and activate their pending project."""
    approved_at = timezone.now()
    project.acceptance_items.filter(
        state=AcceptanceItem.State.SUBMITTED,
    ).update(
        state=AcceptanceItem.State.APPROVED,
        approved_at=approved_at,
    )
    return _transition(
        project,
        Project.Status.CRITERIA_PENDING,
        Project.Status.ACTIVE,
    )


def _transition_acceptance_item(
    item,
    expected_states,
    target_state,
    *,
    additional_guards=None,
    **field_updates,
):
    """Move an acceptance item across one guarded workflow edge."""
    expected_state = item.state
    if expected_state not in expected_states:
        raise InvalidTransition(
            f"Cannot transition acceptance item from {expected_state} "
            f"to {target_state}."
        )
    updates = {"state": target_state, **field_updates}
    guarded_items = AcceptanceItem.objects.filter(
        pk=item.pk,
        state=expected_state,
    )
    if additional_guards:
        guarded_items = guarded_items.filter(**additional_guards)
    updated_count = guarded_items.update(**updates)
    if updated_count == 0:
        try:
            item.refresh_from_db()
        except AcceptanceItem.DoesNotExist:
            raise InvalidTransition(
                "Acceptance item no longer exists."
            ) from None
        raise InvalidTransition(
            "Acceptance item state changed before transition could be saved."
        )
    for field_name, value in updates.items():
        setattr(item, field_name, value)
    return item


def submit_acceptance_item_for_approval(item):
    """Submit one draft acceptance item for client approval."""
    return _transition_acceptance_item(
        item,
        {AcceptanceItem.State.DRAFT},
        AcceptanceItem.State.SUBMITTED,
        submitted_at=timezone.now(),
    )


@transaction.atomic
def submit_acceptance_items_for_approval(items):
    """Submit an atomic subset of draft acceptance items."""
    return [submit_acceptance_item_for_approval(item) for item in items]


def pull_back_acceptance_item(item):
    """Return one submitted acceptance item to editable draft state."""
    return _transition_acceptance_item(
        item,
        {AcceptanceItem.State.SUBMITTED},
        AcceptanceItem.State.DRAFT,
        submitted_at=None,
    )


def approve_acceptance_item(item):
    """Approve one submitted acceptance item."""
    return _transition_acceptance_item(
        item,
        {AcceptanceItem.State.SUBMITTED},
        AcceptanceItem.State.APPROVED,
        approved_at=timezone.now(),
    )


@transaction.atomic
def approve_acceptance_items(items):
    """Approve an atomic subset of submitted acceptance items."""
    return [approve_acceptance_item(item) for item in items]


def suspend_acceptance_item(item):
    """Suspend one submitted or approved acceptance item."""
    return _transition_acceptance_item(
        item,
        {
            AcceptanceItem.State.SUBMITTED,
            AcceptanceItem.State.APPROVED,
        },
        AcceptanceItem.State.SUSPENDED,
    )


def resume_acceptance_item(item):
    """Resume a suspended item to its timestamp-derived prior state."""
    target_state = (
        AcceptanceItem.State.APPROVED
        if item.approved_at is not None
        else AcceptanceItem.State.SUBMITTED
    )
    return _transition_acceptance_item(
        item,
        {AcceptanceItem.State.SUSPENDED},
        target_state,
        additional_guards={"approved_at": item.approved_at},
    )


def withdraw_acceptance_item(item):
    """Withdraw one approved acceptance item permanently."""
    return _transition_acceptance_item(
        item,
        {AcceptanceItem.State.APPROVED},
        AcceptanceItem.State.WITHDRAWN,
    )


def acceptance_item_locked(item):
    """Return whether an acceptance item's criterion is not editable."""
    return item.state != AcceptanceItem.State.DRAFT


@transaction.atomic
def delete_acceptance_item(item):
    """Delete never-approved scope only while its project remains mutable."""
    guard = {
        "approved_at__isnull": True,
        "project__status__in": (
            Project.Status.DRAFT,
            Project.Status.CRITERIA_PENDING,
            Project.Status.ACTIVE,
        ),
    }
    AcceptanceStep.objects.filter(
        item_id=item.pk,
        item__approved_at__isnull=True,
        item__project__status__in=guard["project__status__in"],
    ).delete()
    guarded_items = AcceptanceItem.objects.filter(
        pk=item.pk,
        **guard,
    )
    # The normal collector evaluates this guarded queryset before deleting now
    # that AcceptanceStep cascades from it, opening a check/delete race. Every
    # child relation must be deleted manually above before issuing this guarded
    # DELETE; the related-object tripwire test enforces that complete set.
    deleted_count = guarded_items._raw_delete(guarded_items.db)
    if deleted_count == 0:
        current_item = AcceptanceItem.objects.filter(pk=item.pk).values(
            "approved_at",
        ).first()
        if current_item is None:
            raise InvalidTransition("Acceptance item no longer exists.")
        if current_item["approved_at"] is not None:
            raise InvalidTransition(
                "Client-approved acceptance items cannot be deleted."
            )
        raise InvalidTransition(
            "Acceptance items cannot be deleted after project delivery."
        )


def create_acceptance_step(item, text, order):
    """Create an ordered step while its parent criterion remains draft."""
    if acceptance_item_locked(item):
        raise InvalidTransition(
            "Acceptance steps can only be created while their item is draft."
        )
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Acceptance step text must not be empty.")
    return AcceptanceStep.objects.create(
        item=item,
        text=text,
        order=order,
    )


def update_acceptance_step(step, text, order):
    """Update step structure in one statement guarded by parent draft state."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Acceptance step text must not be empty.")
    updated_count = AcceptanceStep.objects.filter(
        pk=step.pk,
        item__state=AcceptanceItem.State.DRAFT,
    ).update(text=text, order=order)
    if updated_count == 0:
        current_step = AcceptanceStep.objects.filter(pk=step.pk).values(
            "item__state",
        ).first()
        if current_step is None:
            raise InvalidTransition("Acceptance step no longer exists.")
        raise InvalidTransition(
            "Acceptance steps can only be changed while their item is draft."
        )
    step.text = text
    step.order = order
    return step


def delete_acceptance_step(step):
    """Delete a step in one statement guarded by parent draft state."""
    deleted_count, _ = AcceptanceStep.objects.filter(
        pk=step.pk,
        item__state=AcceptanceItem.State.DRAFT,
    ).delete()
    if deleted_count == 0:
        current_step = AcceptanceStep.objects.filter(pk=step.pk).values(
            "item__state",
        ).first()
        if current_step is None:
            raise InvalidTransition("Acceptance step no longer exists.")
        raise InvalidTransition(
            "Acceptance steps can only be deleted while their item is draft."
        )


def set_acceptance_step_done(step, is_done):
    """Set progress in one statement guarded by approved active scope."""
    if not isinstance(is_done, bool):
        raise ValueError("is_done must be a boolean.")
    updated_count = AcceptanceStep.objects.filter(
        pk=step.pk,
        item__state=AcceptanceItem.State.APPROVED,
        item__project__status=Project.Status.ACTIVE,
    ).update(is_done=is_done)
    if updated_count == 0:
        current_step = AcceptanceStep.objects.filter(pk=step.pk).values(
            "item__state",
            "item__project__status",
        ).first()
        if current_step is None:
            raise InvalidTransition("Acceptance step no longer exists.")
        if current_step["item__state"] != AcceptanceItem.State.APPROVED:
            raise InvalidTransition(
                "Acceptance step progress requires an approved item."
            )
        raise InvalidTransition(
            "Acceptance step progress can only change while the project is active."
        )
    step.is_done = is_done
    return step


def acceptance_step_locked(step):
    """Return whether an acceptance step's structure is not editable."""
    return acceptance_item_locked(step.item)


def mark_delivered(project):
    """Mark an active project as delivered and ready to attest."""
    if project.status != Project.Status.ACTIVE:
        raise InvalidTransition(
            f"Cannot transition from {project.status} to {Project.Status.DELIVERED}."
        )
    if project.acceptance_items.filter(
        state__in=(
            AcceptanceItem.State.DRAFT,
            AcceptanceItem.State.SUBMITTED,
        )
    ).exists():
        raise InvalidTransition(
            "Draft or submitted acceptance items block delivery."
        )
    if not project.acceptance_items.filter(
        state=AcceptanceItem.State.APPROVED,
    ).exists():
        raise InvalidTransition(
            "At least one approved acceptance item is required before delivery."
        )
    if project.acceptance_items.filter(
        state=AcceptanceItem.State.APPROVED,
        is_passed=False,
    ).exists():
        raise InvalidTransition("Every delivery item must pass before delivery.")
    if has_open_change_orders(project):
        raise InvalidTransition(
            "Proposed change orders must be resolved before delivery."
        )
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


def has_open_change_orders(project):
    """Return whether the project has an unresolved change order."""
    return project.change_orders.filter(
        status=ChangeOrder.Status.PROPOSED,
    ).exists()


def propose_change_order(project, description, amount_cents, timeline_days):
    """Create a proposed price or timeline adjustment for active work."""
    if project.status != Project.Status.ACTIVE:
        raise InvalidTransition(
            "Change orders can only be proposed for active projects."
        )
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Change order description must not be empty.")
    if amount_cents is None or amount_cents < 0:
        raise ValueError("Change order amount must not be negative.")
    if timeline_days is None or timeline_days < 0:
        raise ValueError("Change order timeline must not be negative.")
    return ChangeOrder.objects.create(
        project=project,
        description=description,
        amount_cents=amount_cents,
        timeline_days=timeline_days,
        status=ChangeOrder.Status.PROPOSED,
    )


def _resolve_change_order(change_order, target_status):
    """Resolve one proposed change order with the requested decision."""
    if change_order.status != ChangeOrder.Status.PROPOSED:
        raise InvalidTransition(
            f"Cannot transition change order from {change_order.status} "
            f"to {target_status}."
        )
    change_order.status = target_status
    change_order.resolved_at = timezone.now()
    change_order.save(update_fields=("status", "resolved_at"))
    return change_order


def approve_change_order(change_order):
    """Approve a proposed change order."""
    return _resolve_change_order(change_order, ChangeOrder.Status.APPROVED)


def decline_change_order(change_order):
    """Decline a proposed change order."""
    return _resolve_change_order(change_order, ChangeOrder.Status.DECLINED)


def _normalized_skills(skills_csv):
    """Return sorted unique skill slugs parsed from comma-separated input."""
    return sorted(
        {
            slugify(tag.strip())
            for tag in skills_csv.split(",")
            if slugify(tag.strip())
        }
    )


def canonical_payload(project):
    """Build the deterministic signed snapshot for a project."""
    acceptance_items = []
    item_rows = project.acceptance_items.order_by("order", "pk").values(
        "id",
        "text",
        "is_passed",
        "evidence_url",
        "state",
        "submitted_at",
        "approved_at",
    )
    steps_by_item_id = defaultdict(list)
    step_rows = AcceptanceStep.objects.filter(
        item__project=project,
    ).order_by("item_id", "order", "pk").values(
        "item_id",
        "text",
        "is_done",
    )
    for step_row in step_rows:
        item_id = step_row.pop("item_id")
        steps_by_item_id[item_id].append(step_row)
    for item_row in item_rows:
        item_id = item_row.pop("id")
        item_row["submitted_at"] = (
            item_row["submitted_at"].isoformat()
            if item_row["submitted_at"] is not None
            else None
        )
        item_row["approved_at"] = (
            item_row["approved_at"].isoformat()
            if item_row["approved_at"] is not None
            else None
        )
        item_row["steps"] = steps_by_item_id[item_id]
        acceptance_items.append(item_row)
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
        "skills": _normalized_skills(project.skills_csv),
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


def verify_payload_hash(attestation) -> bool:
    """Return whether a signed payload still matches its stored hash."""
    return compute_payload_hash(attestation.payload) == attestation.payload_hash


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
    if project.acceptance_items.filter(
        state__in=(
            AcceptanceItem.State.DRAFT,
            AcceptanceItem.State.SUBMITTED,
        )
    ).exists():
        raise InvalidTransition(
            "Draft or submitted acceptance items block signing."
        )
    if not project.acceptance_items.filter(
        state=AcceptanceItem.State.APPROVED,
    ).exists():
        raise InvalidTransition(
            "At least one approved acceptance item is required before signing."
        )
    if project.acceptance_items.filter(
        state=AcceptanceItem.State.APPROVED,
    ).exclude(is_passed=True).exists():
        raise InvalidTransition("Every acceptance item must pass before signing.")

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
        if not verify_payload_hash(attestation):
            continue
        project_tags = set(attestation.payload.get("skills", []))
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
    candidates = (
        Attestation.objects.filter(
            project__owner=profile,
            project__status=Project.Status.ATTESTED,
            is_current=True,
            is_disputed=False,
        )
        .select_related("project")
        .order_by("-signed_at")
    )
    return [
        attestation
        for attestation in candidates
        if verify_payload_hash(attestation)
    ]


def disputed_count(profile):
    """Return the number of disputed attestations for annotation."""
    return Attestation.objects.filter(
        project__owner=profile,
        is_current=True,
        is_disputed=True,
    ).count()

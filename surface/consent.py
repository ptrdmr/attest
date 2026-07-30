"""Consent-critical helpers shared by token and portal client doorways."""

from hashlib import sha256
import json

from django.db import transaction

from ledger import services
from ledger.models import AcceptanceItem, Project

APPROVAL_ACCEPTED = "accepted"
APPROVAL_EMPTY = "empty"
APPROVAL_STALE = "stale"


def _submitted_batch_fingerprint(items):
    """Hash submitted item identity, timestamp, and complete step scope."""
    submitted_entries = sorted(
        [
            item.pk,
            (
                item.submitted_at.isoformat()
                if item.submitted_at is not None
                else "__missing_submitted_at__"
            ),
            [
                [step.pk, step.text, step.order, step.is_done]
                for step in item.steps.all()
            ],
        ]
        for item in items
    )
    serialized_entries = json.dumps(submitted_entries, separators=(",", ":"))
    return sha256(serialized_entries.encode("utf-8")).hexdigest()


def _approve_submitted_batch(project, submitted_fingerprint):
    """Atomically approve the exact submitted batch or report why it was not."""
    with transaction.atomic():
        submitted_items = list(
            project.acceptance_items.select_for_update().filter(
                state=AcceptanceItem.State.SUBMITTED
            ).prefetch_related("steps")
        )
        if not submitted_items:
            return APPROVAL_EMPTY
        if submitted_fingerprint != _submitted_batch_fingerprint(submitted_items):
            return APPROVAL_STALE
        try:
            # One savepoint over both writes keeps partial consent from committing.
            with transaction.atomic():
                services.approve_acceptance_items(submitted_items)
                if project.status == Project.Status.CRITERIA_PENDING:
                    services.approve_criteria(project)
        except services.InvalidTransition:
            return APPROVAL_STALE
    return APPROVAL_ACCEPTED


def _sign_delivery_record(
    *,
    project,
    client_email,
    client_name_typed,
    signature_meta,
):
    """Sign one delivered record through the guarded Ledger service."""
    return services.sign_attestation(
        project=project,
        client_email=client_email,
        client_name_typed=client_name_typed,
        signature_meta=signature_meta,
    )

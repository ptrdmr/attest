from django.conf import settings
from django.db import models
from django.utils import timezone


class ImmutableAttestation(Exception):
    """Indicate an attempted mutation of signed attestation data."""


class Profile(models.Model):
    """Store the public identity associated with one authenticated user."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    handle = models.SlugField(unique=True)
    display_name = models.CharField(max_length=255)
    headline = models.CharField(max_length=255, blank=True)
    bio = models.TextField(blank=True)
    location = models.CharField(max_length=120, blank=True)
    website_url = models.URLField(blank=True)
    is_public = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    def __str__(self):
        """Return the profile's public handle."""
        return self.handle


class Project(models.Model):
    """Represent a freelancer project whose state changes through services."""

    class Status(models.TextChoices):
        """Enumerate valid project workflow states."""

        DRAFT = "draft", "Draft"
        CRITERIA_PENDING = "criteria_pending", "Criteria pending"
        ACTIVE = "active", "Active"
        DELIVERED = "delivered", "Delivered"
        ATTESTED = "attested", "Attested"
        DISPUTED = "disputed", "Disputed"

    owner = models.ForeignKey(
        Profile,
        on_delete=models.CASCADE,
        related_name="projects",
    )
    title = models.CharField(max_length=255)
    client_name = models.CharField(max_length=255)
    client_email = models.EmailField()
    brief = models.TextField()
    skills_csv = models.CharField(max_length=500, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    revision_limit = models.PositiveSmallIntegerField(default=2)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        """Return the project title."""
        return self.title


class AcceptanceItem(models.Model):
    """Store one ordered, verifiable acceptance criterion."""

    class State(models.TextChoices):
        """Enumerate valid per-item approval states."""

        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        APPROVED = "approved", "Approved"
        SUSPENDED = "suspended", "Suspended"
        WITHDRAWN = "withdrawn", "Withdrawn"

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="acceptance_items",
    )
    text = models.TextField()
    order = models.PositiveIntegerField()
    state = models.CharField(
        max_length=10,
        choices=State.choices,
        default=State.DRAFT,
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    is_passed = models.BooleanField(null=True, blank=True)
    evidence_url = models.URLField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """Order criteria stably and forbid duplicate positions."""

        ordering = ("order", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=("project", "order"),
                name="unique_acceptance_item_order_per_project",
            )
        ]

    def __str__(self):
        """Return the acceptance criterion text."""
        return self.text


class AcceptanceStep(models.Model):
    """Store one ordered unit of work beneath an acceptance criterion."""

    item = models.ForeignKey(
        AcceptanceItem,
        on_delete=models.CASCADE,
        related_name="steps",
    )
    text = models.TextField()
    order = models.PositiveIntegerField()
    is_done = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """Order steps stably and forbid duplicate positions per criterion."""

        ordering = ("order", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=("item", "order"),
                name="unique_acceptance_step_order_per_item",
            )
        ]

    def __str__(self):
        """Return the acceptance step text."""
        return self.text


class ChangeOrder(models.Model):
    """Record a proposed adjustment to project price or timeline."""

    class Status(models.TextChoices):
        """Enumerate valid change-order decisions."""

        PROPOSED = "proposed", "Proposed"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="change_orders",
    )
    description = models.TextField()
    amount_cents = models.IntegerField()
    timeline_days = models.IntegerField()
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PROPOSED,
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Reject negative change-order amounts and timelines."""

        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_cents__gte=0),
                name="change_order_amount_cents_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(timeline_days__gte=0),
                name="change_order_timeline_days_non_negative",
            ),
        ]

    def __str__(self):
        """Return the change-order description."""
        return self.description


class AttestationQuerySet(models.QuerySet):
    """Block bulk mutation or deletion of signed attestation data."""

    def update(self, **kwargs):
        """Apply a bulk update unless it targets immutable fields."""
        blocked = set(kwargs) & set(Attestation.IMMUTABLE_FIELDS)
        if blocked:
            raise ImmutableAttestation(
                "Signed attestation fields cannot be bulk-updated."
            )
        return super().update(**kwargs)

    def delete(self):
        """Reject bulk deletion of signed attestation rows."""
        raise ImmutableAttestation("Signed attestations cannot be deleted.")


class Attestation(models.Model):
    """Store an immutable signed snapshot and mutable dispute markers."""

    project = models.ForeignKey(
        Project,
        on_delete=models.PROTECT,
        related_name="attestations",
        db_index=True,
    )
    payload = models.JSONField()
    payload_hash = models.CharField(max_length=64)
    client_email = models.EmailField()
    client_name_typed = models.CharField(max_length=255)
    signed_at = models.DateTimeField(default=timezone.now, editable=False)
    signature_meta = models.JSONField(default=dict)
    is_disputed = models.BooleanField(default=False)
    disputed_at = models.DateTimeField(null=True, blank=True)
    is_current = models.BooleanField(default=True)
    amended_from = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="amendments",
        null=True,
        blank=True,
    )

    IMMUTABLE_FIELDS = (
        "payload",
        "payload_hash",
        "client_email",
        "client_name_typed",
        "signed_at",
    )

    objects = AttestationQuerySet.as_manager()

    class Meta:
        """Allow one current attestation per project across amendments."""

        constraints = [
            models.UniqueConstraint(
                fields=("project",),
                condition=models.Q(is_current=True),
                name="unique_current_attestation_per_project",
            )
        ]

    def __str__(self):
        """Return a non-PII attestation label."""
        return f"Attestation {self.pk or 'unsaved'}"

    def save(self, *args, **kwargs):
        """Persist the row unless existing signed fields were changed."""
        if self.pk:
            stored = type(self).objects.get(pk=self.pk)
            changed = any(
                getattr(stored, field) != getattr(self, field)
                for field in self.IMMUTABLE_FIELDS
            )
            if changed:
                raise ImmutableAttestation(
                    "Signed attestation fields cannot be changed."
                )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Reject deletion of a signed attestation row."""
        raise ImmutableAttestation("Signed attestations cannot be deleted.")


class CapabilityTag(models.Model):
    """Cache an attested skill count derived from signed projects."""

    profile = models.ForeignKey(
        Profile,
        on_delete=models.CASCADE,
        related_name="capability_tags",
    )
    name = models.SlugField()
    attested_count = models.PositiveIntegerField()
    last_attested_at = models.DateTimeField()

    class Meta:
        """Prevent duplicate capability tags for one profile."""

        constraints = [
            models.UniqueConstraint(
                fields=("profile", "name"),
                name="unique_capability_tag_per_profile",
            )
        ]
        ordering = ("name",)

    def __str__(self):
        """Return the capability tag name."""
        return self.name

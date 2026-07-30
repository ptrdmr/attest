from django.contrib import admin, messages

from .models import (
    AcceptanceItem,
    Attestation,
    CapabilityTag,
    ChangeOrder,
    Profile,
    Project,
)
from .services import InvalidTransition, flag_dispute, resolve_dispute


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    """Administer freelancer public profiles."""

    list_display = ("handle", "display_name", "created_at")
    search_fields = ("handle", "display_name")
    readonly_fields = ("created_at",)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    """Inspect projects while reserving status changes for services."""

    list_display = ("title", "owner", "status", "updated_at")
    list_filter = ("status",)
    search_fields = ("title", "owner__handle")
    readonly_fields = ("status", "created_at", "updated_at")


@admin.register(AcceptanceItem)
class AcceptanceItemAdmin(admin.ModelAdmin):
    """Administer project acceptance criteria."""

    list_display = ("project", "order", "is_passed", "created_at")
    list_filter = ("is_passed",)
    readonly_fields = ("created_at",)


@admin.register(ChangeOrder)
class ChangeOrderAdmin(admin.ModelAdmin):
    """Administer project change orders."""

    list_display = ("project", "status", "amount_cents", "created_at")
    list_filter = ("status",)
    readonly_fields = ("created_at",)


@admin.register(Attestation)
class AttestationAdmin(admin.ModelAdmin):
    """Expose dispute controls without permitting signed-data edits."""

    list_display = (
        "project",
        "signed_at",
        "is_current",
        "is_disputed",
        "disputed_at",
    )
    list_filter = ("is_current", "is_disputed")
    readonly_fields = (
        "project",
        "payload",
        "payload_hash",
        "client_email",
        "client_name_typed",
        "signed_at",
        "signature_meta",
        "is_current",
        "amended_from",
        "is_disputed",
        "disputed_at",
    )
    actions = ("flag_selected_disputes", "resolve_selected_disputes")

    def _apply_dispute_action(
        self,
        request,
        queryset,
        transition,
        outcome,
    ):
        """Apply one dispute transition per selected project and report results."""
        processed_project_ids = set()
        transitioned_count = 0
        duplicate_count = 0
        invalid_count = 0
        for attestation in queryset.select_related("project"):
            if attestation.project_id in processed_project_ids:
                duplicate_count += 1
                continue
            processed_project_ids.add(attestation.project_id)
            try:
                transition(attestation.project)
            except InvalidTransition:
                invalid_count += 1
                continue
            transitioned_count += 1
        level = messages.WARNING if invalid_count else messages.SUCCESS
        self.message_user(
            request,
            f"{transitioned_count} project(s) {outcome}; "
            f"{duplicate_count} duplicate attestation(s) skipped; "
            f"{invalid_count} invalid transition(s) skipped.",
            level=level,
        )

    @admin.action(description="Flag selected projects as disputed")
    def flag_selected_disputes(self, request, queryset):
        """Flag each selected attestation's project through the service."""
        self._apply_dispute_action(
            request,
            queryset,
            flag_dispute,
            "flagged as disputed",
        )

    @admin.action(description="Resolve selected project disputes")
    def resolve_selected_disputes(self, request, queryset):
        """Resolve each selected attestation's project through the service."""
        self._apply_dispute_action(
            request,
            queryset,
            resolve_dispute,
            "resolved",
        )

    def has_add_permission(self, request):
        """Require attestations to be created by the signing service."""
        return False

    def has_delete_permission(self, request, obj=None):
        """Forbid deleting signed rows; the ledger is append-only."""
        return False


@admin.register(CapabilityTag)
class CapabilityTagAdmin(admin.ModelAdmin):
    """Display service-derived capability tags as read-only data."""

    list_display = (
        "profile",
        "name",
        "attested_count",
        "last_attested_at",
    )
    readonly_fields = (
        "profile",
        "name",
        "attested_count",
        "last_attested_at",
    )

    def has_add_permission(self, request):
        """Require capability tags to be created by derivation."""
        return False

    def has_delete_permission(self, request, obj=None):
        """Require capability tags to be deleted by derivation."""
        return False

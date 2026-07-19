from django.contrib import admin

from .models import (
    AcceptanceItem,
    Attestation,
    CapabilityTag,
    ChangeOrder,
    Profile,
    Project,
)


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

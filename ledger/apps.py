from django.apps import AppConfig


class LedgerConfig(AppConfig):
    name = 'ledger'

    def ready(self):
        """Connect attestation cache-freshness signals."""
        from django.db.models.signals import post_save

        from .models import Attestation
        from .services import recompute_capability_tags_after_attestation_save

        post_save.connect(
            recompute_capability_tags_after_attestation_save,
            sender=Attestation,
            dispatch_uid="ledger.recompute_capability_tags_after_attestation_save",
        )

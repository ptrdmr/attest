"""Neutral delivery-row preparation shared by client-facing surfaces."""


def _delivery_rows(project):
    """Return ordered acceptance items with prefetched steps and progress flags."""
    acceptance_items = list(
        project.acceptance_items.prefetch_related("steps").order_by("order", "pk")
    )
    rows = []
    for item in acceptance_items:
        steps = list(item.steps.all())
        rows.append(
            {
                "item": item,
                "steps": steps,
                "has_undone_steps": any(not step.is_done for step in steps),
            }
        )
    return rows

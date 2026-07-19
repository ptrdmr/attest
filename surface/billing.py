"""Session-backed billing provider used until a real gateway is approved."""

from django.conf import settings

PRO_PLAN_LABEL = "$29/mo Pro"
PROJECT_PACK_LABEL = "$39/project pack"

_PRO_SESSION_KEY = "attest_billing_pro"
_PACK_CREDITS_SESSION_KEY = "attest_billing_project_pack_credits"


def billing_stub_enabled():
    """Return whether local session billing controls may grant entitlement."""
    return bool(getattr(settings, "ATTEST_BILLING_STUB_MODE", False))


def has_pro_subscription(request):
    """Return whether this session has an active stub Pro subscription."""
    return bool(request.session.get(_PRO_SESSION_KEY, False))


def project_pack_credits(request):
    """Return the number of project-pack credits in this session."""
    return max(0, int(request.session.get(_PACK_CREDITS_SESSION_KEY, 0)))


def has_project_entitlement(request):
    """Return whether this request may create a project."""
    return has_pro_subscription(request) or project_pack_credits(request) > 0


def grant_pro_subscription(request):
    """Activate stub Pro entitlement for the current session."""
    if not billing_stub_enabled():
        return False
    request.session[_PRO_SESSION_KEY] = True
    return True


def grant_project_pack(request):
    """Add one stub project-pack credit to the current session."""
    if not billing_stub_enabled():
        return False
    request.session[_PACK_CREDITS_SESSION_KEY] = project_pack_credits(request) + 1
    return True


def consume_project_entitlement(request):
    """Consume one project-pack credit unless Pro covers the project.

    Persists the session immediately so a concurrent tab is less likely to
    reuse the same project-pack credit. Returns True on success.
    """
    if has_pro_subscription(request):
        return True
    credits = project_pack_credits(request)
    if credits < 1:
        return False
    request.session[_PACK_CREDITS_SESSION_KEY] = credits - 1
    request.session.modified = True
    request.session.save()
    return True


def restore_project_pack_credit(request):
    """Refund one project-pack credit after a failed project create."""
    if has_pro_subscription(request):
        return
    request.session[_PACK_CREDITS_SESSION_KEY] = project_pack_credits(request) + 1
    request.session.modified = True

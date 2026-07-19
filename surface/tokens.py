"""Signed, expiring access tokens for account-free client views."""

from django.core import signing

from ledger.models import Project

CLIENT_TOKEN_MAX_AGE = 14 * 24 * 60 * 60
CLIENT_TOKEN_PURPOSES = frozenset({"review", "sign"})


def _salt_for(purpose):
    """Return the isolated signing salt for an allowed token purpose."""
    if purpose not in CLIENT_TOKEN_PURPOSES:
        raise ValueError("Unsupported client token purpose.")
    return f"surface.client.{purpose}"


def make_client_token(project, purpose):
    """Sign a project identifier for one client-facing purpose."""
    return signing.dumps(
        {"project_id": project.pk},
        salt=_salt_for(purpose),
        compress=True,
    )


def read_client_token(token, purpose):
    """Validate a client token and return its project."""
    payload = signing.loads(
        token,
        salt=_salt_for(purpose),
        max_age=CLIENT_TOKEN_MAX_AGE,
    )
    if not isinstance(payload, dict):
        raise signing.BadSignature("Invalid client token payload.")
    project_id = payload.get("project_id")
    if type(project_id) is not int:
        raise signing.BadSignature("Invalid client token payload.")
    return Project.objects.get(pk=project_id)

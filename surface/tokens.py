"""Signed, expiring access tokens for account-free client views."""

from uuid import uuid4

from django.core import signing

from ledger.models import ChangeOrder, Project

CLIENT_TOKEN_MAX_AGE = 14 * 24 * 60 * 60
CLIENT_TOKEN_PURPOSES = frozenset({"change_order", "review", "sign"})
PORTAL_TOKEN_MAX_AGE = 60 * 60
PORTAL_TOKEN_SALT = "surface.client.portal"


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


def make_portal_token(email):
    """Sign a normalized client email in the isolated portal namespace."""
    return signing.dumps(
        {"email": email.strip().lower(), "nonce": uuid4().hex},
        salt=PORTAL_TOKEN_SALT,
        compress=True,
    )


def read_portal_token(token):
    """Validate a short-lived portal token and return its normalized email."""
    payload = signing.loads(
        token,
        salt=PORTAL_TOKEN_SALT,
        max_age=PORTAL_TOKEN_MAX_AGE,
    )
    if not isinstance(payload, dict):
        raise signing.BadSignature("Invalid portal token payload.")
    email = payload.get("email")
    nonce = payload.get("nonce")
    if (
        not isinstance(email, str)
        or not email
        or email != email.strip().lower()
        or not isinstance(nonce, str)
        or len(nonce) != 32
        or any(char not in "0123456789abcdef" for char in nonce)
    ):
        raise signing.BadSignature("Invalid portal token payload.")
    return email


def make_change_order_token(change_order):
    """Sign one proposal identifier for client review."""
    return signing.dumps(
        {
            "project_id": change_order.project_id,
            "change_order_id": change_order.pk,
        },
        salt=_salt_for("change_order"),
        compress=True,
    )


def read_change_order_token(token):
    """Validate a proposal token and return its project-bound change order."""
    payload = signing.loads(
        token,
        salt=_salt_for("change_order"),
        max_age=CLIENT_TOKEN_MAX_AGE,
    )
    if not isinstance(payload, dict):
        raise signing.BadSignature("Invalid client token payload.")
    project_id = payload.get("project_id")
    change_order_id = payload.get("change_order_id")
    if type(project_id) is not int or type(change_order_id) is not int:
        raise signing.BadSignature("Invalid client token payload.")
    return ChangeOrder.objects.select_related("project").get(
        pk=change_order_id,
        project_id=project_id,
    )

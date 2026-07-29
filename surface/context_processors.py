"""Template context derived from account-free client session state."""

from .client_auth import client_email_from_session


def client_session(request):
    """Expose only an unexpired client email without mutating the session."""
    return {"attest_client_email": client_email_from_session(request.session)}

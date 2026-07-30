"""Helpers for passwordless freelancer authentication."""

from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import IntegrityError, transaction
from django.utils.text import slugify

from ledger.models import Profile

from .signed_links import (
    consume_token_once,
    normalize_magic_login_token,
    prepare_magic_login_token,
    require_unconsumed_token,
    single_use_cache_key,
)

MAGIC_LOGIN_SALT = "surface.magic-login"
# Long enough for console copy/paste and slow email clients; still short-lived.
MAGIC_LOGIN_MAX_AGE = 60 * 60
MAGIC_LOGIN_USED_NAMESPACE = "surface.magic-login.used"


def _unique_handle(seed):
    """Build an available profile handle from an email local part."""
    local_part = seed.partition("@")[0]
    base = slugify(local_part)[:40] or "freelancer"
    candidate = base
    suffix = 2
    while Profile.objects.filter(handle=candidate).exists():
        candidate = f"{base[:35]}-{suffix}"
        suffix += 1
    return candidate


def ensure_profile(user):
    """Return the user's profile, creating one for accounts that lack it.

    Covers freelancers on first login and pre-existing accounts (such as
    superusers created via ``createsuperuser``) that have no Profile row.
    """
    try:
        return user.profile
    except Profile.DoesNotExist:
        pass
    seed = user.email or user.username
    display_name = seed.partition("@")[0] or "Freelancer"
    try:
        with transaction.atomic():
            return Profile.objects.create(
                user=user,
                handle=_unique_handle(seed),
                display_name=display_name,
            )
    except IntegrityError:
        # A concurrent request claimed the handle or created this profile.
        # Retry once with a collision-proof handle; if the user row already
        # has a profile, that second insert fails too and we fetch it.
        fresh_handle = f"{slugify(display_name)[:28] or 'freelancer'}-{uuid4().hex[:8]}"
        try:
            with transaction.atomic():
                return Profile.objects.create(
                    user=user,
                    handle=fresh_handle,
                    display_name=display_name,
                )
        except IntegrityError:
            return Profile.objects.get(user=user)


def get_or_create_freelancer(email):
    """Return the user for an email, creating user and profile on first login.

    The username is the normalized email itself. Django's unique username
    constraint therefore serializes concurrent first logins for the same new
    email: the losing INSERT raises IntegrityError and we re-fetch the winner.
    Residual case: accounts whose username is not their email (for example
    admin accounts) are matched only by the initial email lookup, so two such
    rows sharing one email cannot be deduplicated here.
    """
    normalized_email = email.strip().lower()
    user_model = get_user_model()
    user = user_model.objects.filter(email__iexact=normalized_email).first()
    if user is None:
        try:
            with transaction.atomic():
                user = user_model.objects.create_user(
                    username=normalized_email,
                    email=normalized_email,
                    password=None,
                )
        except IntegrityError:
            user = user_model.objects.get(username=normalized_email)
    ensure_profile(user)
    return user


def make_magic_login_token(user):
    """Sign a user primary key for a short-lived magic login link."""
    signer = signing.TimestampSigner(salt=MAGIC_LOGIN_SALT)
    return signer.sign(f"{user.pk}.{uuid4().hex}")


def _prepare_magic_login_token(token):
    """Normalize one token in DEBUG and otherwise preserve its raw value."""
    return prepare_magic_login_token(token)


def _read_magic_login_token(token):
    """Validate one prepared magic login token and return its active user."""
    signer = signing.TimestampSigner(salt=MAGIC_LOGIN_SALT)
    signed_value = signer.unsign(token, max_age=MAGIC_LOGIN_MAX_AGE)
    user_value, separator, nonce = signed_value.partition(".")
    if not separator or len(nonce) != 32 or any(
        char not in "0123456789abcdef" for char in nonce
    ):
        raise signing.BadSignature("Invalid login token payload.")
    try:
        user_pk = int(user_value)
    except (TypeError, ValueError) as error:
        raise signing.BadSignature("Invalid login token payload.") from error
    return get_user_model().objects.get(pk=user_pk, is_active=True)


def _magic_login_cache_key(token):
    """Return the consumed-token cache key for one prepared token."""
    return single_use_cache_key(token, MAGIC_LOGIN_USED_NAMESPACE)


def read_unconsumed_magic_login_token(token):
    """Validate an unused token without consuming it."""
    prepared_token = _prepare_magic_login_token(token)
    user = _read_magic_login_token(prepared_token)
    require_unconsumed_token(prepared_token, MAGIC_LOGIN_USED_NAMESPACE)
    return user


def read_and_consume_magic_login_token(token):
    """Validate and atomically consume a single-use magic login token."""
    prepared_token = _prepare_magic_login_token(token)
    user = _read_magic_login_token(prepared_token)
    # Multi-worker production assumes a shared CACHES backend; LocMemCache
    # makes this atomic single-use guarantee process-local only.
    consume_token_once(
        prepared_token,
        MAGIC_LOGIN_USED_NAMESPACE,
        MAGIC_LOGIN_MAX_AGE + 60,
    )
    return user

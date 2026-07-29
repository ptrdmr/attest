"""Identity-free helpers shared by signed, single-use login links."""

from hashlib import sha256

from django.conf import settings
from django.core import signing
from django.core.cache import cache


def normalize_magic_login_token(token):
    """Repair tokens mangled by console quoted-printable copy and wrapping."""
    if token is None:
        return ""
    cleaned = "".join(str(token).split())
    if cleaned.startswith("3D") and ":" in cleaned[2:]:
        cleaned = cleaned[2:]
    return cleaned.replace("=", "")


def prepare_magic_login_token(token):
    """Normalize a copied console token in DEBUG and preserve it otherwise."""
    if settings.DEBUG:
        return normalize_magic_login_token(token)
    return token


def single_use_cache_key(token, namespace):
    """Return a non-reversible cache key for one signed token namespace."""
    token_digest = sha256(str(token).encode("utf-8")).hexdigest()
    return f"{namespace}:{token_digest}"


def require_unconsumed_token(token, namespace):
    """Raise when a prepared token already has a consumption marker."""
    if cache.get(single_use_cache_key(token, namespace)):
        raise signing.BadSignature("Login token has already been used.")


def consume_token_once(token, namespace, timeout):
    """Atomically mark a prepared token consumed for the supplied lifetime."""
    cache_key = single_use_cache_key(token, namespace)
    if not cache.add(cache_key, True, timeout=timeout):
        raise signing.BadSignature("Login token has already been used.")

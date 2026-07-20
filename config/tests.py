"""Verifier tests for production fail-closed settings (M6c)."""

import importlib
import os
import sys
import unittest
from unittest.mock import patch

SETTINGS_MODULE = "config.settings"
DEV_SECRET_KEY = "django-insecure-dev-only-do-not-use-in-production"


def _reload_settings(env, *, clear=True):
    """Import or reload config.settings under a patched os.environ."""
    with patch.dict(os.environ, env, clear=clear):
        module = sys.modules.get(SETTINGS_MODULE)
        if module is None:
            module = importlib.import_module(SETTINGS_MODULE)
        else:
            module = importlib.reload(module)
    return module


def _restore_settings_env(original_env):
    """Restore os.environ and reload settings so sys.modules stays consistent."""
    os.environ.clear()
    os.environ.update(original_env)
    importlib.reload(sys.modules[SETTINGS_MODULE])


class SettingsFailClosedTests(unittest.TestCase):
    """Settings module enforces fail-closed guards at import time."""

    def setUp(self):
        self._original_env = os.environ.copy()

    def tearDown(self):
        _restore_settings_env(self._original_env)

    def test_debug_unset_without_secret_key_raises(self):
        with self.assertRaisesRegex(RuntimeError, "DJANGO_SECRET_KEY"):
            _reload_settings(
                {
                    "DJANGO_ALLOWED_HOSTS": "example.com",
                }
            )

    def test_production_without_allowed_hosts_raises(self):
        with self.assertRaisesRegex(RuntimeError, "DJANGO_ALLOWED_HOSTS"):
            _reload_settings(
                {
                    "DJANGO_DEBUG": "0",
                    "DJANGO_SECRET_KEY": "production-test-secret",
                }
            )

    def test_production_with_required_env_is_hardened(self):
        settings = _reload_settings(
            {
                "DJANGO_DEBUG": "0",
                "DJANGO_SECRET_KEY": "production-test-secret",
                "DJANGO_ALLOWED_HOSTS": "example.com,www.example.com",
                "DJANGO_CSRF_TRUSTED_ORIGINS": "https://example.com,https://www.example.com",
            }
        )
        self.assertFalse(settings.DEBUG)
        self.assertTrue(settings.SECURE_SSL_REDIRECT)
        self.assertTrue(settings.SESSION_COOKIE_SECURE)
        self.assertTrue(settings.CSRF_COOKIE_SECURE)
        self.assertFalse(settings.ATTEST_BILLING_STUB_MODE)
        self.assertEqual(
            settings.CSRF_TRUSTED_ORIGINS,
            ["https://example.com", "https://www.example.com"],
        )

    def test_debug_mode_allows_insecure_dev_defaults(self):
        settings = _reload_settings({"DJANGO_DEBUG": "1"})
        self.assertTrue(settings.DEBUG)
        self.assertEqual(settings.SECRET_KEY, DEV_SECRET_KEY)
        self.assertTrue(settings.ATTEST_BILLING_STUB_MODE)

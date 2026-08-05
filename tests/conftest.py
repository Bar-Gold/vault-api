"""Hermetic test environment.

Two jobs, both of which must happen before any `app.*` module is imported (pytest loads
this file first, and the config singletons are built at import time):

1. provide every **required** setting, so `global_conf` / `v1/vault/conf` construct;
2. shadow the optional `AUTH_*` knobs to empty, so a developer `.env` cannot change test
   outcomes by adding a second inbound-auth material (which makes the library's mode
   selection ambiguous and fails app creation — the suite configures HS256 only).
"""
import os

# 32+ bytes to avoid PyJWT's short-key warning.
HS256_SECRET = "test-secret-test-secret-test-secret"

_BASE_ENV = {
    "BITBUCKET_URL": "https://bitbucket.test",
    "BITBUCKET_TOKEN": "bb-token",
    "VAULT_VALUES_REPO_PROJECT_KEY": "INFRA",
    "VAULT_VALUES_REPO_SLUG": "vault-values",
    "VAULT_VALUES_REPO_BASE_BRANCH": "master",
    # Keep the suite fast: no real waiting between polls.
    "CI_POLL_INTERVAL_SECONDS": "0",
    "CI_PIPELINE_START_TIMEOUT_SECONDS": "0",
    "CI_PIPELINE_TIMEOUT_SECONDS": "0",
    "AUTH_ENABLED": "true",
    "AUTH_HS256_SECRET": HS256_SECRET,
}
for _key, _value in _BASE_ENV.items():
    os.environ.setdefault(_key, _value)

_SHADOW_EMPTY = (
    # Keep HS256 the only inbound material.
    "AUTH_JWKS_URL",
    "AUTH_OIDC_ISSUER",
    "AUTH_PUBLIC_KEY_PEM",
    "AUTH_PUBLIC_KEY_PATH",
    "AUTH_AUDIENCE",
    "AUTH_ISSUER",
    # No preconfigured outbound SSO.
    "AUTH_SSO_TOKEN_URL",
    "AUTH_SSO_CLIENT_ID",
    "AUTH_SSO_CLIENT_SECRET",
    "AUTH_SSO_SCOPE",
    "AUTH_SSO_AUDIENCE",
)
for _key in _SHADOW_EMPTY:
    os.environ[_key] = ""

from datetime import datetime, timedelta, timezone  # noqa: E402

import jwt  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture
def make_token():
    def _make(secret=HS256_SECRET, **claims):
        payload = {"sub": "svc-vault", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}
        payload.update(claims)
        return jwt.encode(payload, secret, algorithm="HS256")

    return _make


@pytest.fixture
def auth_headers(make_token):
    return {"Authorization": f"Bearer {make_token()}"}

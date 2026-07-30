"""Request validation — the cheapest place to stop a bad mount path or policy."""
import pytest
from pydantic import ValidationError

from app.v1.vault.conf import config
from app.v1.vault.schemas import KVVersion, VaultKVCreate, VaultKVCreateSpec


def _spec(**overrides):
    values = {"app_name": "myapp", "owner": "team-dl@example.com"}
    values.update(overrides)
    return VaultKVCreateSpec(**values)


# --------------------------------------------------------------------------- #
# app_name — it becomes a Vault path segment and a policy name
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "app_name",
    [
        "myapp",
        "my-app",
        "my-app-1",
        "a1",
        # Callers name their own mounts, so a name may be a multi-segment path.
        "payments/secrets",
        "payments/prod/api-secrets",
        "a/b/c/d",
    ],
)
def test_valid_app_names(app_name):
    assert _spec(app_name=app_name).app_name == app_name


@pytest.mark.parametrize(
    "app_name",
    [
        "MyApp",  # uppercase
        "my_app",  # underscore
        "-myapp",  # leading dash
        "myapp-",  # trailing dash
        "my--app",  # doubled dash
        "my app",  # space
        "",  # empty
        # The name lands in a file path, so traversal and empty segments must not pass.
        "/myapp",  # leading slash
        "myapp/",  # trailing slash
        "my//app",  # empty segment
        "../etc/passwd",  # traversal
        "payments/../../etc",  # traversal mid-path
        ".",
        "..",
        "payments/./secrets",
    ],
)
def test_rejected_app_names(app_name):
    with pytest.raises(ValidationError):
        _spec(app_name=app_name)


def test_app_name_length_is_capped():
    with pytest.raises(ValidationError):
        _spec(app_name="a" * 129)


# --------------------------------------------------------------------------- #
# defaults
# --------------------------------------------------------------------------- #
def test_defaults_come_from_conf():
    spec = _spec()
    assert spec.kv_version == KVVersion.V2
    assert spec.max_versions == config.DEFAULT_KV_MAX_VERSIONS
    assert spec.delete_version_after == config.DEFAULT_DELETE_VERSION_AFTER
    assert spec.readers == []
    assert spec.writers == []
    assert spec.description is None


def test_owner_is_required():
    with pytest.raises(ValidationError):
        VaultKVCreateSpec(app_name="myapp")


# --------------------------------------------------------------------------- #
# kv version and retention
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("version", [1, 2])
def test_supported_kv_versions(version):
    assert int(_spec(kv_version=version).kv_version) == version


@pytest.mark.parametrize("version", [0, 3, "two"])
def test_unsupported_kv_versions(version):
    with pytest.raises(ValidationError):
        _spec(kv_version=version)


@pytest.mark.parametrize("max_versions", [0, -1, 101])
def test_max_versions_is_bounded(max_versions):
    with pytest.raises(ValidationError):
        _spec(max_versions=max_versions)


@pytest.mark.parametrize("duration", ["720h", "30m", "45s", "10d"])
def test_valid_vault_durations(duration):
    assert _spec(delete_version_after=duration).delete_version_after == duration


@pytest.mark.parametrize("duration", ["720", "1x", "h", "1.5h", "720 h"])
def test_invalid_vault_durations(duration):
    with pytest.raises(ValidationError):
        _spec(delete_version_after=duration)


# --------------------------------------------------------------------------- #
# entity lists — a blank entry would render an empty policy binding
# --------------------------------------------------------------------------- #
def test_blank_entities_are_rejected():
    with pytest.raises(ValidationError):
        _spec(readers=["group/a", "  "])

    with pytest.raises(ValidationError):
        _spec(writers=[""])


def test_entities_are_stripped():
    assert _spec(readers=[" group/a "]).readers == ["group/a"]


# --------------------------------------------------------------------------- #
# the request wrapper
# --------------------------------------------------------------------------- #
def test_metadata_is_required(create_spec):
    with pytest.raises(ValidationError):
        VaultKVCreate(spec=create_spec)


def test_request_carries_the_infra_coordinates(payload):
    assert payload.metadata.environment == "prod"
    assert payload.metadata.project == "payments"

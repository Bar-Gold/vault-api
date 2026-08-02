"""Request validation — the cheapest place to stop a bad name or an empty edit."""
import pytest
from pydantic import ValidationError

from app.v1.vault.schemas import VaultKVCreate, VaultKVUpdate


def _create(**overrides):
    values = {"kv_name": "myapp", "kv_description": "payments secrets"}
    values.update(overrides)
    return VaultKVCreate(**values)


# --------------------------------------------------------------------------- #
# kv_name — it becomes the committed file's path, so it is also a security boundary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kv_name",
    [
        "myapp",
        "my-app",
        "my-app-1",
        "a1",
        # Callers name their own KVs, so a name may be a multi-segment path.
        "payments/secrets",
        "payments/prod/api-secrets",
    ],
)
def test_valid_names(kv_name):
    assert _create(kv_name=kv_name).kv_name == kv_name


@pytest.mark.parametrize(
    "kv_name",
    [
        "MyApp",  # uppercase
        "my_app",  # underscore
        "-myapp",  # leading dash
        "myapp-",  # trailing dash
        "my--app",  # doubled dash
        "my app",  # space
        "",  # empty
        # The name reaches a file path, so traversal and empty segments must not pass.
        "/myapp",
        "myapp/",
        "my//app",
        "../etc/passwd",
        "payments/../../etc",
        "..",
        "payments/./secrets",
    ],
)
def test_rejected_names(kv_name):
    with pytest.raises(ValidationError):
        _create(kv_name=kv_name)


def test_name_length_is_capped():
    with pytest.raises(ValidationError):
        _create(kv_name="a" * 129)


# --------------------------------------------------------------------------- #
# kv_description
# --------------------------------------------------------------------------- #
def test_description_is_required():
    with pytest.raises(ValidationError):
        VaultKVCreate(kv_name="myapp")


@pytest.mark.parametrize("description", ["", "   "])
def test_blank_descriptions_are_rejected(description):
    with pytest.raises(ValidationError):
        _create(kv_description=description)


def test_description_is_stripped():
    assert _create(kv_description="  padded  ").kv_description == "padded"


def test_description_length_is_capped():
    with pytest.raises(ValidationError):
        _create(kv_description="a" * 257)


def test_create_takes_exactly_two_fields():
    """The whole point: a caller supplies a name and a description, nothing else."""
    assert set(VaultKVCreate.model_fields) == {"kv_name", "kv_description"}


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #
def test_update_accepts_either_field():
    assert VaultKVUpdate(kv_description="new").kv_description == "new"
    assert VaultKVUpdate(owner="a@b.c").owner == "a@b.c"


def test_empty_update_is_rejected():
    """An edit that specifies nothing would open a pull request that changes nothing."""
    with pytest.raises(ValidationError):
        VaultKVUpdate()

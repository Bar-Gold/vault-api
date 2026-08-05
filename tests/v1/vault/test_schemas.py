"""Request validation — the cheapest place to stop a bad name, file or role."""
import re

import pytest
from pydantic import ValidationError

from app.v1.vault.schemas import (
    ALLOWED_ROLE_KEYS,
    FILE_PATTERN,
    VaultKVCreate,
    VaultKVUpdate,
)

ROLES = {"read": ["app01.corp.example.com"]}


def _create(**overrides):
    values = {
        "file": "payments",
        "kv_name": "myapp",
        "kv_description": "payments secrets",
        "roles": ROLES,
    }
    values.update(overrides)
    return VaultKVCreate(**values)


# --------------------------------------------------------------------------- #
# file — the only request field that reaches a filesystem path
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("file", ["payments", "infra", "team-a", "a1"])
def test_valid_files(file):
    assert _create(file=file).file == file


@pytest.mark.parametrize(
    "file",
    [
        "Payments",  # uppercase
        "my_file",  # underscore
        "-lead",
        "trail-",
        "my--file",
        "my file",
        "",
        # The file lands in the committed path, so traversal must not pass.
        "../etc/passwd",
        "a/b",
        "/payments",
        "payments/",
        "..",
        ".",
    ],
)
def test_rejected_files(file):
    with pytest.raises(ValidationError):
        _create(file=file)


# --------------------------------------------------------------------------- #
# kv_name — a value in the document, but also a path segment in the routes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kv_name", ["myapp", "my-app", "my-app-1", "a1"])
def test_valid_names(kv_name):
    assert _create(kv_name=kv_name).kv_name == kv_name


@pytest.mark.parametrize(
    "kv_name",
    [
        "MyApp",
        "my_app",
        "-myapp",
        "myapp-",
        "my--app",
        "my app",
        "",
        # A slash would make the {file}/{kv_name} route split ambiguous.
        "payments/secrets",
        "a/b/c",
        "../etc",
        "..",
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
        VaultKVCreate(file="payments", kv_name="myapp", roles=ROLES)


@pytest.mark.parametrize("description", ["", "   "])
def test_blank_descriptions_are_rejected(description):
    with pytest.raises(ValidationError):
        _create(kv_description=description)


def test_description_is_stripped():
    assert _create(kv_description="  padded  ").kv_description == "padded"


def test_description_length_is_capped():
    with pytest.raises(ValidationError):
        _create(kv_description="a" * 257)


# --------------------------------------------------------------------------- #
# roles — required, because a store nobody can reach is not useful
# --------------------------------------------------------------------------- #
def test_roles_are_required():
    with pytest.raises(ValidationError):
        VaultKVCreate(file="payments", kv_name="myapp", kv_description="d")


@pytest.mark.parametrize("role", ["read", "write"])
def test_either_role_alone_is_valid(role):
    assert _create(roles={role: ["a.example.com"]}).roles == {role: ["a.example.com"]}


def test_both_roles_together_are_valid():
    """A host that only reads and one that also writes are separate entries."""
    roles = {"read": ["reader.example.com"], "write": ["writer.example.com"]}

    assert _create(roles=roles).roles == roles


@pytest.mark.parametrize(
    "roles",
    [
        {},  # no roles at all
        {"read": []},  # role with no hosts
        {"write": []},
        {"read": ["  "]},  # blank host
        {"read": ["a.example.com", ""]},  # one blank among good ones
        {"read/write": ["a.example.com"]},  # the combined key is NOT a role
        {"admin": ["a.example.com"]},
        {"readwrite": ["a.example.com"]},
        {"read": ["a.example.com"], "admin": ["b.example.com"]},  # one bad among good
        {"read": ["dup.example.com", "dup.example.com"]},  # repeated host
    ],
)
def test_rejected_roles(roles):
    with pytest.raises(ValidationError):
        _create(roles=roles)


def test_hosts_are_stripped():
    assert _create(roles={"read": ["  a.example.com  "]}).roles == {
        "read": ["a.example.com"]
    }


def test_several_hosts_are_kept_in_order():
    hosts = ["b.example.com", "a.example.com", "c.example.com"]

    assert _create(roles={"read": hosts}).roles["read"] == hosts


def test_the_allowed_role_set_is_the_single_place_to_change():
    """`read` and `write` are separate keys; the combined `read/write` is not a role."""
    assert ALLOWED_ROLE_KEYS == frozenset({"read", "write"})


def test_create_takes_exactly_four_fields():
    assert set(VaultKVCreate.model_fields) == {
        "file",
        "kv_name",
        "kv_description",
        "roles",
    }


# --------------------------------------------------------------------------- #
# `file` is optional, and defaults to the store's own name
#
# A caller should not have to remember which file their store lives in, so the normal
# request is three fields. That makes an unrecognised field sharper than it used to be: a
# mistyped `file` no longer fails on a missing required field, so every request model
# forbids extras rather than dropping them.
# --------------------------------------------------------------------------- #
def test_file_defaults_to_the_store_name():
    payload = VaultKVCreate(
        kv_name="athena-passwords", kv_description="d", roles=ROLES
    )

    assert payload.file == "athena-passwords"


def test_an_explicit_file_is_kept():
    payload = VaultKVCreate(
        file="athena", kv_name="athena-passwords", kv_description="d", roles=ROLES
    )

    assert payload.file == "athena"


def test_the_defaulted_file_is_still_a_legal_file_name():
    """`kv_name` and `file` carry the same single-segment shape, so this always holds."""
    payload = VaultKVCreate(kv_name="a-b-c9", kv_description="d", roles=ROLES)

    assert re.fullmatch(FILE_PATTERN, payload.file)


def test_an_explicit_null_file_also_defaults():
    payload = VaultKVCreate(
        file=None, kv_name="athena-passwords", kv_description="d", roles=ROLES
    )

    assert payload.file == "athena-passwords"


def test_a_malformed_explicit_file_is_still_rejected():
    with pytest.raises(ValidationError):
        VaultKVCreate(file="Bad_File", kv_name="ok", kv_description="d", roles=ROLES)


def test_an_unknown_create_field_is_rejected():
    """Ignoring it would answer 201 for a request nobody meant to send."""
    with pytest.raises(ValidationError):
        VaultKVCreate(
            kv_name="ok", kv_description="d", roles=ROLES, environment="prod"
        )


def test_a_mistyped_file_is_rejected_rather_than_silently_defaulted():
    """The reason `extra: forbid` matters more now than it did: `fil` would otherwise be
    dropped and the store would land in kv/ok.yaml instead of kv/athena.yaml."""
    with pytest.raises(ValidationError):
        VaultKVCreate(fil="athena", kv_name="ok", kv_description="d", roles=ROLES)


def test_an_unknown_update_field_is_rejected():
    with pytest.raises(ValidationError):
        VaultKVUpdate(kv_description="new", file="athena")


def test_an_unknown_binding_field_is_rejected():
    from app.v1.vault.schemas import K8sServiceAccountCreate

    with pytest.raises(ValidationError):
        K8sServiceAccountCreate(
            service_account="vault", namespace="athena", cluster="dev", capability="read"
        )


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #
def test_update_accepts_either_field():
    assert VaultKVUpdate(kv_description="new").kv_description == "new"
    assert VaultKVUpdate(roles=ROLES).roles == ROLES


def test_empty_update_is_rejected():
    """An edit that specifies nothing would open a pull request that changes nothing."""
    with pytest.raises(ValidationError):
        VaultKVUpdate()


def test_update_rejects_a_blank_description():
    with pytest.raises(ValidationError):
        VaultKVUpdate(kv_description="   ")


def test_update_validates_roles_the_same_way():
    with pytest.raises(ValidationError):
        VaultKVUpdate(roles={"admin": ["a.example.com"]})


def test_update_cannot_rename():
    """The name is fixed at creation; it comes from the URL, never the body."""
    assert "kv_name" not in VaultKVUpdate.model_fields

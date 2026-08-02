"""Naming, the committed document, and the edits applied to it.

These are pure functions, so nothing here imports the library or touches HTTP.
"""
import pytest
import yaml

from app.helpers import (
    build_branch_name,
    build_kv_values,
    render_values_yaml,
    slugify_mount_path,
    update_kv_metadata,
    values_file_path,
    yaml_data_equals,
)


# --------------------------------------------------------------------------- #
# naming
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kv_name,expected",
    [
        ("myapp", "myapp"),
        ("payments/vault-secrets", "payments-vault-secrets"),
        ("a/b/c", "a-b-c"),
        ("/leading/", "leading"),
    ],
)
def test_slugify_flattens_slashes(kv_name, expected):
    assert slugify_mount_path(kv_name) == expected


def test_branch_name_is_prefixed():
    assert build_branch_name("myapp", "ab12cd34", "vault-kv") == "vault-kv/myapp-ab12cd34"


def test_branch_name_flattens_a_multi_segment_kv():
    """A slash would nest the ref, and git cannot hold both a/b and a/b/c."""
    branch = build_branch_name("payments/vault-secrets", "ab12cd34", "vault-kv")

    assert branch == "vault-kv/payments-vault-secrets-ab12cd34"
    assert branch.count("/") == 1


@pytest.mark.parametrize(
    "kv_name,expected",
    [
        ("myapp", "kv/myapp.yaml"),
        ("payments/vault-secrets", "kv/payments/vault-secrets.yaml"),
    ],
)
def test_values_file_path(kv_name, expected):
    assert values_file_path("kv", kv_name) == expected


def test_values_file_path_strips_stray_slashes():
    assert values_file_path("/kv/", "myapp") == "kv/myapp.yaml"


# --------------------------------------------------------------------------- #
# the committed document
# --------------------------------------------------------------------------- #
def test_build_kv_values_is_just_the_name_and_description():
    """This service writes a file and watches pipelines; it models nothing about Vault."""
    assert build_kv_values("myapp", "payments secrets") == {
        "kvname": "myapp",
        "description": "payments secrets",
    }


def test_rendered_yaml_round_trips():
    values = build_kv_values("myapp", "payments secrets")
    rendered = render_values_yaml(values)

    assert yaml.safe_load(rendered) == values
    assert rendered == "kvname: myapp\ndescription: payments secrets\n"


def test_multi_line_strings_render_as_block_scalars():
    """Quoted-and-escaped multi-line scalars are unreadable in a pull request diff."""
    rendered = render_values_yaml(
        build_kv_values("myapp", "payments secrets\nowned by the payments team\n")
    )

    assert "description: |" in rendered
    assert "\\n" not in rendered


# --------------------------------------------------------------------------- #
# comparison
# --------------------------------------------------------------------------- #
def test_yaml_data_equals_ignores_ordering():
    assert yaml_data_equals("a: 1\nb: [2, 1]\n", "b: [1, 2]\na: 1\n")


def test_yaml_data_equals_detects_difference():
    assert not yaml_data_equals("a: 1\n", "a: 2\n")


# --------------------------------------------------------------------------- #
# edits
#
# The edit must leave its input alone, so the caller can diff old against new and skip a
# no-op commit.
# --------------------------------------------------------------------------- #
def _simple():
    return {"kvname": "myapp", "description": "old"}


def test_update_replaces_description_and_owner():
    updated = update_kv_metadata(_simple(), description="new", owner="new@example.com")

    assert updated["description"] == "new"
    assert updated["owner"] == "new@example.com"


def test_update_leaves_omitted_fields_alone():
    updated = update_kv_metadata(_simple(), description="new")

    assert "owner" not in updated
    assert updated["kvname"] == "myapp"


def test_update_never_touches_the_name():
    """Renaming means migrating secrets in Vault, not editing a field."""
    assert update_kv_metadata(_simple(), description="new")["kvname"] == "myapp"


def test_update_does_not_mutate_its_input():
    original = _simple()
    update_kv_metadata(original, description="new")

    assert original["description"] == "old"

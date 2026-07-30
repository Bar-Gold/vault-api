"""Naming, the committed document, and the edits applied to it.

These are pure functions, so nothing here imports the library or touches HTTP.
"""
import pytest
import yaml

from app.helpers import (
    ValuesEditError,
    add_group_binding,
    add_kubernetes_auth,
    build_branch_name,
    build_kubernetes_role_name,
    build_kv_values,
    find_policy_name,
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


def test_kubernetes_role_name_defaults_to_the_flattened_name():
    assert build_kubernetes_role_name("payments/vault-secrets") == "payments-vault-secrets"


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
    rendered = render_values_yaml({"rules": 'path "a/b" {\n  capabilities = ["read"]\n}\n'})

    assert "rules: |" in rendered
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
# Every edit must leave its input alone, so the caller can diff old against new and skip
# a no-op commit.
# --------------------------------------------------------------------------- #
def _simple():
    return {"kvname": "myapp", "description": "old"}


def _with_policies():
    """A file whose shape has grown policies — what the k8s/group edits need."""
    return {
        "kvname": "myapp",
        "description": "old",
        "policies": [
            {"name": "myapp-read", "entities": ["group/r"]},
            {"name": "myapp-write", "entities": ["group/w"]},
        ],
    }


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


def test_find_policy_name_picks_the_capability():
    assert find_policy_name(_with_policies(), "read") == "myapp-read"
    assert find_policy_name(_with_policies(), "write") == "myapp-write"


def test_find_policy_name_raises_on_a_file_without_policies():
    with pytest.raises(ValuesEditError):
        find_policy_name(_simple(), "read")


def test_add_kubernetes_auth_binds_the_named_policy():
    updated = add_kubernetes_auth(
        _with_policies(),
        role="r",
        service_accounts=["sa"],
        namespaces=["ns"],
        capability="write",
    )

    assert updated["kubernetes_auth"] == [
        {
            "role": "r",
            "service_accounts": ["sa"],
            "namespaces": ["ns"],
            "policies": ["myapp-write"],
        }
    ]


def test_add_kubernetes_auth_includes_ttl_only_when_given():
    with_ttl = add_kubernetes_auth(
        _with_policies(), role="r", service_accounts=["sa"], namespaces=["ns"],
        capability="read", ttl="24h",
    )
    without = add_kubernetes_auth(
        _with_policies(), role="r", service_accounts=["sa"], namespaces=["ns"],
        capability="read",
    )

    assert with_ttl["kubernetes_auth"][0]["ttl"] == "24h"
    assert "ttl" not in without["kubernetes_auth"][0]


def test_add_kubernetes_auth_replaces_a_role_of_the_same_name():
    once = add_kubernetes_auth(
        _with_policies(), role="r", service_accounts=["old"], namespaces=["ns"],
        capability="read",
    )
    twice = add_kubernetes_auth(
        once, role="r", service_accounts=["new"], namespaces=["ns"], capability="read"
    )

    assert len(twice["kubernetes_auth"]) == 1
    assert twice["kubernetes_auth"][0]["service_accounts"] == ["new"]


def test_add_kubernetes_auth_appends_a_different_role():
    once = add_kubernetes_auth(
        _with_policies(), role="a", service_accounts=["sa"], namespaces=["ns"],
        capability="read",
    )
    twice = add_kubernetes_auth(
        once, role="b", service_accounts=["sa"], namespaces=["ns"], capability="read"
    )

    assert [role["role"] for role in twice["kubernetes_auth"]] == ["a", "b"]


def test_add_kubernetes_auth_needs_a_policy_to_bind():
    with pytest.raises(ValuesEditError):
        add_kubernetes_auth(
            _simple(), role="r", service_accounts=["sa"], namespaces=["ns"],
            capability="read",
        )


def test_add_group_binding_appends_to_the_right_policy():
    updated = add_group_binding(_with_policies(), group=r"AD\payments-ro", capability="read")

    policies = {p["name"]: p for p in updated["policies"]}
    assert policies["myapp-read"]["entities"] == ["group/r", r"AD\payments-ro"]
    assert policies["myapp-write"]["entities"] == ["group/w"]


def test_add_group_binding_is_idempotent():
    updated = add_group_binding(_with_policies(), group="group/r", capability="read")

    assert yaml_data_equals(updated, _with_policies())


def test_add_group_binding_does_not_mutate_its_input():
    original = _with_policies()
    add_group_binding(original, group=r"AD\payments-ro", capability="read")

    assert original["policies"][0]["entities"] == ["group/r"]


def test_add_group_binding_needs_a_policy():
    with pytest.raises(ValuesEditError):
        add_group_binding(_simple(), group="g", capability="read")

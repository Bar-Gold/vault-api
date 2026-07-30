"""Naming convention, policy rendering and values-file shape.

These are pure functions, so the payload is duck-typed — no library import needed.
"""
import types

import pytest
import yaml

from app.helpers import (
    ValuesEditError,
    add_group_binding,
    add_kubernetes_auth,
    build_branch_name,
    build_kubernetes_role_name,
    build_mount_path,
    build_policy_name,
    build_values_data,
    break_mount_path,
    find_policy_name,
    render_read_policy,
    render_values_yaml,
    render_write_policy,
    update_mount_metadata,
    values_file_path,
    yaml_data_equals,
)


def _payload(**overrides):
    spec = {
        "app_name": "myapp",
        "owner": "team-dl@example.com",
        "kv_version": 2,
        "description": None,
        "max_versions": 10,
        "delete_version_after": None,
        "readers": ["group/readers"],
        "writers": ["group/writers"],
    }
    spec.update(overrides)
    return types.SimpleNamespace(spec=types.SimpleNamespace(**spec))


# --------------------------------------------------------------------------- #
# naming
# --------------------------------------------------------------------------- #
def test_mount_path_is_team_environment_app():
    assert build_mount_path("kingmagen", "prod", "myapp") == "kingmagen/prod/myapp"


def test_break_mount_path_round_trips():
    assert break_mount_path("kingmagen/prod/myapp") == ("kingmagen", "prod", "myapp")


def test_policy_name_includes_capability():
    assert build_policy_name("kingmagen", "prod", "myapp", "read") == "kingmagen-prod-myapp-read"


def test_branch_name_is_prefixed_and_unique():
    assert build_branch_name("prod", "myapp", "ab12cd34", "vault-kv") == "vault-kv/prod-myapp-ab12cd34"


def test_values_file_path_strips_stray_slashes():
    assert values_file_path("/kv/", "prod", "myapp") == "kv/prod/myapp.yaml"


# --------------------------------------------------------------------------- #
# policy rendering
# --------------------------------------------------------------------------- #
def test_read_policy_v2_covers_data_and_metadata():
    policy = render_read_policy("kingmagen/prod/myapp", 2)
    assert 'path "kingmagen/prod/myapp/data/*"' in policy
    assert 'path "kingmagen/prod/myapp/metadata/*"' in policy
    assert '"read", "list"' in policy
    # Read-only must never grant mutation.
    assert "create" not in policy
    assert "update" not in policy
    assert "delete" not in policy


def test_write_policy_v2_grants_mutation_on_data():
    policy = render_write_policy("kingmagen/prod/myapp", 2)
    assert '"create", "read", "update", "delete", "list"' in policy
    assert 'path "kingmagen/prod/myapp/metadata/*"' in policy


def test_kv_v1_policy_uses_flat_path():
    policy = render_read_policy("kingmagen/prod/myapp", 1)
    assert 'path "kingmagen/prod/myapp/*"' in policy
    assert "/data/" not in policy
    assert "/metadata/" not in policy


# --------------------------------------------------------------------------- #
# values file
# --------------------------------------------------------------------------- #
def test_build_values_data_shape():
    mount_path, values = build_values_data(_payload(), "kingmagen", "prod")

    assert mount_path == "kingmagen/prod/myapp"
    assert values["mount"]["path"] == "kingmagen/prod/myapp"
    assert values["mount"]["type"] == "kv"
    assert values["mount"]["options"] == {"version": "2"}
    assert values["mount"]["config"]["max_versions"] == 10
    assert [policy["name"] for policy in values["policies"]] == [
        "kingmagen-prod-myapp-read",
        "kingmagen-prod-myapp-write",
    ]
    assert values["policies"][0]["entities"] == ["group/readers"]
    assert values["policies"][1]["entities"] == ["group/writers"]
    assert values["metadata"] == {
        "app": "myapp",
        "team": "kingmagen",
        "environment": "prod",
        "owner": "team-dl@example.com",
    }


def test_build_values_data_defaults_the_description():
    _, values = build_values_data(_payload(), "kingmagen", "prod")
    assert values["mount"]["description"] == "KV store for myapp (prod)"


def test_build_values_data_includes_delete_version_after_when_set():
    _, values = build_values_data(_payload(delete_version_after="720h"), "kingmagen", "prod")
    assert values["mount"]["config"]["delete_version_after"] == "720h"


def test_kv_v1_has_no_versioning_config():
    _, values = build_values_data(_payload(kv_version=1), "kingmagen", "prod")
    assert values["mount"]["options"] == {"version": "1"}
    assert "config" not in values["mount"]


def test_rendered_yaml_round_trips():
    _, values = build_values_data(_payload(), "kingmagen", "prod")
    rendered = render_values_yaml(values)
    assert yaml.safe_load(rendered) == values
    # Key order is preserved so review diffs stay readable.
    assert rendered.index("mount:") < rendered.index("policies:")


# --------------------------------------------------------------------------- #
# comparison
# --------------------------------------------------------------------------- #
def test_yaml_data_equals_ignores_ordering():
    assert yaml_data_equals("a: 1\nb: [2, 1]\n", "b: [1, 2]\na: 1\n")


def test_yaml_data_equals_detects_difference():
    assert not yaml_data_equals("a: 1\n", "a: 2\n")


# --------------------------------------------------------------------------- #
# edits to an existing values file
#
# Every edit must leave its input alone, so the caller can diff old against new and skip
# a no-op commit.
# --------------------------------------------------------------------------- #
def _values():
    return {
        "mount": {"path": "kingmagen/prod/myapp", "description": "old"},
        "policies": [
            {"name": "kingmagen-prod-myapp-read", "entities": ["group/r"]},
            {"name": "kingmagen-prod-myapp-write", "entities": ["group/w"]},
        ],
        "metadata": {"owner": "old@example.com"},
    }


def test_update_mount_metadata_replaces_description_and_owner():
    updated = update_mount_metadata(_values(), description="new", owner="new@example.com")

    assert updated["mount"]["description"] == "new"
    assert updated["metadata"]["owner"] == "new@example.com"


def test_update_mount_metadata_leaves_omitted_fields_alone():
    updated = update_mount_metadata(_values(), description="new")

    assert updated["metadata"]["owner"] == "old@example.com"


def test_update_mount_metadata_does_not_mutate_its_input():
    original = _values()
    update_mount_metadata(original, description="new")

    assert original["mount"]["description"] == "old"


def test_build_kubernetes_role_name():
    assert build_kubernetes_role_name("kingmagen", "prod", "myapp") == "kingmagen-prod-myapp"


def test_find_policy_name_picks_the_capability():
    assert find_policy_name(_values(), "read") == "kingmagen-prod-myapp-read"
    assert find_policy_name(_values(), "write") == "kingmagen-prod-myapp-write"


def test_find_policy_name_raises_when_absent():
    with pytest.raises(ValuesEditError):
        find_policy_name({"policies": [{"name": "unrelated"}]}, "read")


def test_add_kubernetes_auth_binds_the_named_policy():
    updated = add_kubernetes_auth(
        _values(), role="r", service_accounts=["sa"], namespaces=["ns"], capability="write"
    )

    assert updated["kubernetes_auth"] == [
        {
            "role": "r",
            "service_accounts": ["sa"],
            "namespaces": ["ns"],
            "policies": ["kingmagen-prod-myapp-write"],
        }
    ]


def test_add_kubernetes_auth_includes_ttl_only_when_given():
    with_ttl = add_kubernetes_auth(
        _values(), role="r", service_accounts=["sa"], namespaces=["ns"],
        capability="read", ttl="24h",
    )
    without = add_kubernetes_auth(
        _values(), role="r", service_accounts=["sa"], namespaces=["ns"], capability="read"
    )

    assert with_ttl["kubernetes_auth"][0]["ttl"] == "24h"
    assert "ttl" not in without["kubernetes_auth"][0]


def test_add_kubernetes_auth_replaces_a_role_of_the_same_name():
    once = add_kubernetes_auth(
        _values(), role="r", service_accounts=["old"], namespaces=["ns"], capability="read"
    )
    twice = add_kubernetes_auth(
        once, role="r", service_accounts=["new"], namespaces=["ns"], capability="read"
    )

    assert len(twice["kubernetes_auth"]) == 1
    assert twice["kubernetes_auth"][0]["service_accounts"] == ["new"]


def test_add_kubernetes_auth_appends_a_different_role():
    once = add_kubernetes_auth(
        _values(), role="a", service_accounts=["sa"], namespaces=["ns"], capability="read"
    )
    twice = add_kubernetes_auth(
        once, role="b", service_accounts=["sa"], namespaces=["ns"], capability="read"
    )

    assert [role["role"] for role in twice["kubernetes_auth"]] == ["a", "b"]


def test_add_group_binding_appends_to_the_right_policy():
    updated = add_group_binding(_values(), group=r"AD\payments-ro", capability="read")

    policies = {p["name"]: p for p in updated["policies"]}
    assert policies["kingmagen-prod-myapp-read"]["entities"] == [
        "group/r",
        r"AD\payments-ro",
    ]
    assert policies["kingmagen-prod-myapp-write"]["entities"] == ["group/w"]


def test_add_group_binding_is_idempotent():
    updated = add_group_binding(_values(), group="group/r", capability="read")

    assert yaml_data_equals(updated, _values())


def test_add_group_binding_does_not_mutate_its_input():
    original = _values()
    add_group_binding(original, group=r"AD\payments-ro", capability="read")

    assert original["policies"][0]["entities"] == ["group/r"]


def test_add_group_binding_raises_without_the_policy():
    with pytest.raises(ValuesEditError):
        add_group_binding({"policies": [{"name": "unrelated"}]}, group="g", capability="read")

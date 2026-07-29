"""Naming convention, policy rendering and values-file shape.

These are pure functions, so the payload is duck-typed — no library import needed.
"""
import types

import yaml

from app.helpers import (
    build_branch_name,
    build_mount_path,
    build_policy_name,
    build_values_data,
    break_mount_path,
    render_read_policy,
    render_values_yaml,
    render_write_policy,
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

"""Naming, the committed document, and the edits applied to it.

These are pure functions, so nothing here imports the library or touches HTTP.

The document is a *list* of stores under `kvStores`, so most of these assert that an
operation touches exactly one entry and leaves its siblings alone.
"""
import pytest
import yaml

from app.helpers import (
    KVStoreNotFound,
    add_kv_store,
    build_branch_name,
    build_kv_store,
    build_kv_stores_document,
    find_kv_store,
    kv_store_names,
    read_kv_stores,
    render_values_yaml,
    slugify_mount_path,
    update_kv_store,
    values_file_path,
    yaml_data_equals,
)

ROLES = {"read": ["app01.corp.example.com"]}


def _store(name="myapp", description="payments secrets", roles=None):
    return build_kv_store(name, description, roles or ROLES)


def _document(*names):
    return build_kv_stores_document([_store(name=n) for n in names])


# --------------------------------------------------------------------------- #
# naming
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value,expected",
    [("myapp", "myapp"), ("a/b/c", "a-b-c"), ("/leading/", "leading")],
)
def test_slugify_flattens_slashes(value, expected):
    assert slugify_mount_path(value) == expected


def test_branch_name_carries_both_coordinates():
    """A reviewer should see which file and which store a PR touches."""
    assert (
        build_branch_name("payments", "myapp", "ab12cd34", "vault-kv")
        == "vault-kv/payments-myapp-ab12cd34"
    )


def test_branch_name_has_exactly_one_slash():
    """More would nest the ref, and git cannot hold both a/b and a/b/c."""
    assert build_branch_name("payments", "myapp", "ab12", "vault-kv").count("/") == 1


@pytest.mark.parametrize(
    "file,expected", [("payments", "kv/payments.yaml"), ("infra", "kv/infra.yaml")]
)
def test_values_file_path_is_keyed_on_the_file(file, expected):
    """One file holds many stores, so the path comes from the file, not the store name."""
    assert values_file_path("kv", file) == expected


def test_values_file_path_strips_stray_slashes():
    assert values_file_path("/kv/", "payments") == "kv/payments.yaml"


# --------------------------------------------------------------------------- #
# the committed document
# --------------------------------------------------------------------------- #
def test_build_kv_store_is_name_description_roles():
    """The contract with the deploy pipeline. Nothing about mounts or policies."""
    assert _store() == {
        "name": "myapp",
        "description": "payments secrets",
        "roles": {"read": ["app01.corp.example.com"]},
    }


def test_build_kv_store_copies_the_role_lists():
    """A caller mutating its own input must not reach into the built document."""
    hosts = ["a.example.com"]
    store = build_kv_store("myapp", "d", {"read": hosts})
    hosts.append("b.example.com")

    assert store["roles"]["read"] == ["a.example.com"]


def test_document_wraps_the_stores_in_a_list():
    assert _document("one", "two")["kvStores"][0]["name"] == "one"
    assert len(_document("one", "two")["kvStores"]) == 2


def test_rendered_yaml_round_trips():
    document = _document("myapp")

    assert yaml.safe_load(render_values_yaml(document)) == document


def test_rendered_yaml_has_the_expected_shape():
    rendered = render_values_yaml(_document("myapp"))

    assert rendered == (
        "kvStores:\n"
        "- name: myapp\n"
        "  description: payments secrets\n"
        "  roles:\n"
        "    read:\n"
        "    - app01.corp.example.com\n"
    )


def test_multi_line_strings_render_as_block_scalars():
    """Quoted-and-escaped multi-line scalars are unreadable in a pull request diff."""
    rendered = render_values_yaml(
        build_kv_stores_document([_store(description="line one\nline two\n")])
    )

    assert "description: |" in rendered
    assert "\\n" not in rendered


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("empty", [None, {}, {"kvStores": None}, {"kvStores": []}])
def test_read_stores_tolerates_an_empty_document(empty):
    """`kvStores:` with nothing under it is an empty file, not a broken one."""
    assert read_kv_stores(empty) == []


def test_find_returns_the_named_store():
    assert find_kv_store(_document("one", "two"), "two")["name"] == "two"


def test_find_returns_none_when_absent():
    assert find_kv_store(_document("one"), "nope") is None


def test_store_names_lists_every_entry():
    assert kv_store_names(_document("one", "two")) == ["one", "two"]


def test_store_names_skips_malformed_entries():
    """A hand-edited file must not break the duplicate scan."""
    document = {"kvStores": [{"name": "ok"}, "just a string", {"no": "name"}]}

    assert kv_store_names(document) == ["ok"]


# --------------------------------------------------------------------------- #
# appending
# --------------------------------------------------------------------------- #
def test_add_to_a_missing_file_starts_the_list():
    """The first store in a file creates it, so None is a valid input."""
    assert add_kv_store(None, _store())["kvStores"] == [_store()]


def test_add_appends_after_the_existing_stores():
    result = add_kv_store(_document("one"), _store(name="two"))

    assert [s["name"] for s in result["kvStores"]] == ["one", "two"]


def test_add_does_not_mutate_its_input():
    original = _document("one")
    add_kv_store(original, _store(name="two"))

    assert len(original["kvStores"]) == 1


def test_add_deep_copies_so_later_edits_do_not_leak():
    original = _document("one")
    result = add_kv_store(original, _store(name="two"))
    result["kvStores"][0]["description"] = "changed"

    assert original["kvStores"][0]["description"] == "payments secrets"


# --------------------------------------------------------------------------- #
# editing
#
# The edit must leave its input alone, so the caller can diff old against new and skip a
# no-op commit.
# --------------------------------------------------------------------------- #
def test_update_replaces_the_description():
    updated = update_kv_store(_document("one"), "one", description="new")

    assert updated["kvStores"][0]["description"] == "new"


def test_update_replaces_roles_wholesale():
    """Merging would make removing a host impossible."""
    updated = update_kv_store(
        _document("one"), "one", roles={"read": ["only.example.com"]}
    )

    assert updated["kvStores"][0]["roles"] == {"read": ["only.example.com"]}


def test_update_leaves_omitted_fields_alone():
    updated = update_kv_store(_document("one"), "one", description="new")

    assert updated["kvStores"][0]["roles"] == ROLES


def test_update_never_touches_the_name():
    """Renaming means migrating secrets in Vault, not editing a field."""
    assert update_kv_store(_document("one"), "one", description="new")["kvStores"][0]["name"] == "one"


def test_update_leaves_siblings_alone():
    """The whole point of many stores per file."""
    updated = update_kv_store(_document("one", "two"), "two", description="new")

    assert updated["kvStores"][0]["description"] == "payments secrets"
    assert updated["kvStores"][1]["description"] == "new"


def test_update_does_not_mutate_its_input():
    original = _document("one")
    update_kv_store(original, "one", description="new")

    assert original["kvStores"][0]["description"] == "payments secrets"


def test_update_of_an_absent_store_raises():
    with pytest.raises(KVStoreNotFound):
        update_kv_store(_document("one"), "nope", description="new")


# --------------------------------------------------------------------------- #
# comparison
# --------------------------------------------------------------------------- #
def test_yaml_data_equals_ignores_ordering():
    assert yaml_data_equals("a: 1\nb: [2, 1]\n", "b: [1, 2]\na: 1\n")


def test_yaml_data_equals_detects_difference():
    assert not yaml_data_equals("a: 1\n", "a: 2\n")


def test_a_no_op_update_compares_equal():
    """This is what lets the operation skip an empty pull request."""
    document = _document("one")
    updated = update_kv_store(document, "one", description="payments secrets")

    assert yaml_data_equals(document, updated)

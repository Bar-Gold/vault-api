"""Naming, the committed document, and the edits applied to it.

These are pure functions, so nothing here imports the library or touches HTTP.

The document is a *list* of stores under `kvStores`, so most of these assert that an
operation touches exactly one entry and leaves its siblings alone.
"""
import pytest
import yaml

from app.helpers import (
    KVStoreNotFound,
    add_kubernetes_auth_role,
    add_kv_store,
    build_branch_name,
    build_kubernetes_auth_role,
    build_kv_store,
    build_kv_stores_document,
    find_kubernetes_auth_role,
    find_kv_store,
    kubernetes_auth_role_identities,
    kubernetes_auth_role_names,
    kubernetes_auth_roles,
    kv_store_names,
    kv_store_referrers,
    read_kv_stores,
    remove_kubernetes_auth_role,
    remove_kv_store,
    render_values_yaml,
    slugify_mount_path,
    update_kubernetes_auth_role,
    update_kv_store,
    values_file_path,
    yaml_data_equals,
)

ROLES = {"read": ["app01.corp.example.com"]}

# A plausible second top-level key. Nothing in the code knows this name — that is the
# point: the transforms must carry it through without understanding it.
SIBLING_KEY = "kubernetesAuth"
SIBLING_VALUE = [{"name": "myapp-ci", "namespaces": ["payments"]}]


def _store(name="myapp", description="payments secrets", roles=None):
    return build_kv_store(name, description, roles or ROLES)


def _document(*names):
    return build_kv_stores_document([_store(name=n) for n in names])


def _document_with_sibling(*names):
    document = _document(*names)
    document[SIBLING_KEY] = [dict(entry) for entry in SIBLING_VALUE]
    return document


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


def test_update_copies_the_roles_it_is_given():
    """A caller mutating its own input must not reach into the returned document."""
    hosts = ["a.example.com"]
    updated = update_kv_store(_document("one"), "one", roles={"read": hosts})
    hosts.append("b.example.com")

    assert updated["kvStores"][0]["roles"]["read"] == ["a.example.com"]


# --------------------------------------------------------------------------- #
# removing
#
# Same contract as the edit: a new document, the input untouched, and a name that is not
# there is an error rather than a silent success.
# --------------------------------------------------------------------------- #
def test_remove_drops_only_the_named_store():
    result = remove_kv_store(_document("one", "two", "three"), "two")

    assert [s["name"] for s in result["kvStores"]] == ["one", "three"]


def test_remove_does_not_mutate_its_input():
    original = _document("one", "two")
    remove_kv_store(original, "two")

    assert [s["name"] for s in original["kvStores"]] == ["one", "two"]


def test_remove_deep_copies_so_later_edits_do_not_leak():
    original = _document("one", "two")
    result = remove_kv_store(original, "two")
    result["kvStores"][0]["description"] = "changed"

    assert original["kvStores"][0]["description"] == "payments secrets"


def test_removing_the_last_store_leaves_an_empty_list():
    """The file stays and keeps its key: an empty list is a file that declares nothing."""
    result = remove_kv_store(_document("only"), "only")

    assert result == {"kvStores": []}


def test_an_emptied_file_renders_and_reads_back_as_empty():
    """`kvStores: []` has to survive the round trip, or a later create cannot append."""
    rendered = render_values_yaml(remove_kv_store(_document("only"), "only"))

    assert rendered == "kvStores: []\n"
    assert read_kv_stores(yaml.safe_load(rendered)) == []


def test_remove_of_an_absent_store_raises():
    with pytest.raises(KVStoreNotFound):
        remove_kv_store(_document("one"), "nope")


def test_remove_from_an_empty_file_raises():
    with pytest.raises(KVStoreNotFound):
        remove_kv_store({"kvStores": []}, "nope")


# --------------------------------------------------------------------------- #
# sibling top-level keys
#
# The document is a mapping, not just a store list. Every transform replaces its own key
# and copies the rest through, so a key it knows nothing about survives — rebuilding the
# document from the store list alone would erase it, and the erase would look like a
# legitimate diff in the pull request.
# --------------------------------------------------------------------------- #
def test_add_preserves_a_sibling_top_level_key():
    result = add_kv_store(_document_with_sibling("one"), _store(name="two"))

    assert result[SIBLING_KEY] == SIBLING_VALUE
    assert [s["name"] for s in result["kvStores"]] == ["one", "two"]


def test_update_preserves_a_sibling_top_level_key():
    result = update_kv_store(_document_with_sibling("one"), "one", description="new")

    assert result[SIBLING_KEY] == SIBLING_VALUE


def test_remove_preserves_a_sibling_top_level_key():
    result = remove_kv_store(_document_with_sibling("one", "two"), "two")

    assert result[SIBLING_KEY] == SIBLING_VALUE


def test_emptying_the_stores_preserves_a_sibling_top_level_key():
    """Deleting the last store must not take the rest of the file with it."""
    result = remove_kv_store(_document_with_sibling("only"), "only")

    assert result == {"kvStores": [], SIBLING_KEY: SIBLING_VALUE}


def test_a_sibling_top_level_key_is_deep_copied_out_of_the_input():
    original = _document_with_sibling("one")
    result = add_kv_store(original, _store(name="two"))
    result[SIBLING_KEY][0]["name"] = "changed"

    assert original[SIBLING_KEY][0]["name"] == "myapp-ci"


def test_reads_ignore_a_sibling_top_level_key():
    """The scans read `kvStores` and nothing else, so a shared file scans correctly."""
    document = _document_with_sibling("one")

    assert kv_store_names(document) == ["one"]
    assert [s["name"] for s in read_kv_stores(document)] == ["one"]
    assert find_kv_store(document, "myapp-ci") is None


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


# --------------------------------------------------------------------------- #
# Kubernetes auth roles
#
# The second kind of entry. It goes through the same key-parameterised family, so the
# contracts above (new document, input untouched, siblings preserved) are inherited rather
# than reimplemented — these check the *format*: which keys are written, and how the two
# kinds coexist in one file.
# --------------------------------------------------------------------------- #
ACCESS = {"read": ["myapp"]}


def _role(name="myapp-ci", cluster="prod-il-1", **overrides):
    values = {
        "role_name": name,
        "role_description": "CI deployer",
        "service_accounts": ["vault-reader"],
        "namespaces": ["payments"],
        "access": ACCESS,
        "cluster": cluster,
    }
    values.update(overrides)
    return build_kubernetes_auth_role(**values)


def _roles_document(*names):
    return {"kubernetesAuth": [_role(name=n) for n in names]}


def test_build_role_translates_to_the_document_keys():
    """snake_case request fields, camelCase document keys — translated in one place."""
    assert _role() == {
        "name": "myapp-ci",
        "description": "CI deployer",
        "cluster": "prod-il-1",
        "serviceAccounts": ["vault-reader"],
        "namespaces": ["payments"],
        "access": {"read": ["myapp"]},
    }


def test_build_role_writes_nothing_about_policies():
    """The scope boundary: a role names stores and a capability, never a policy."""
    entry = _role(ttl="24h")

    assert "policies" not in entry
    assert entry["access"] == {"read": ["myapp"]}
    assert set(entry) <= {
        "name",
        "description",
        "cluster",
        "serviceAccounts",
        "namespaces",
        "access",
        "ttl",
    }


def test_build_role_omits_an_absent_cluster_and_ttl():
    """A key holding null is harder for a pipeline to read as "not specified"."""
    entry = _role(cluster=None)

    assert "cluster" not in entry
    assert "ttl" not in entry


def test_build_role_keeps_ttl_when_given():
    assert _role(ttl="24h")["ttl"] == "24h"


def test_build_role_copies_the_lists_it_is_given():
    """A caller mutating its own input must not reach into the built entry."""
    accounts = ["vault-reader"]
    entry = build_kubernetes_auth_role(
        "myapp-ci", "d", accounts, ["payments"], {"read": ["myapp"]}
    )
    accounts.append("other")

    assert entry["serviceAccounts"] == ["vault-reader"]


def test_role_reads_mirror_the_store_reads():
    document = _roles_document("one", "two")

    assert [r["name"] for r in kubernetes_auth_roles(document)] == ["one", "two"]
    assert kubernetes_auth_role_names(document) == ["one", "two"]
    assert find_kubernetes_auth_role(document, "two")["name"] == "two"
    assert find_kubernetes_auth_role(document, "nope") is None


@pytest.mark.parametrize("empty", [None, {}, {"kubernetesAuth": None}])
def test_role_reads_tolerate_an_empty_document(empty):
    assert kubernetes_auth_roles(empty) == []
    assert kubernetes_auth_role_names(empty) == []


def test_role_identities_pair_cluster_with_name():
    """The cross-file uniqueness key."""
    assert kubernetes_auth_role_identities(_roles_document("one")) == [
        ("prod-il-1", "one")
    ]


def test_role_identities_use_an_empty_cluster_when_absent():
    """`cluster` is optional, so its absence is itself a coordinate, not a wildcard."""
    document = {"kubernetesAuth": [_role(cluster=None)]}

    assert kubernetes_auth_role_identities(document) == [("", "myapp-ci")]


def test_role_identities_skip_malformed_entries():
    document = {"kubernetesAuth": [{"name": "ok"}, "just a string", {"no": "name"}]}

    assert kubernetes_auth_role_identities(document) == [("", "ok")]


def test_add_role_to_a_missing_file_starts_the_list():
    assert add_kubernetes_auth_role(None, _role())["kubernetesAuth"] == [_role()]


def test_a_file_started_by_a_role_carries_no_empty_store_list():
    """It never asked for a `kvStores: []` sibling, so it must not get one."""
    assert list(add_kubernetes_auth_role(None, _role())) == ["kubernetesAuth"]


def test_update_role_replaces_lists_wholesale():
    """Merging would make removing a namespace impossible."""
    updated = update_kubernetes_auth_role(
        _roles_document("one"), "one", namespaces=["other"]
    )

    assert updated["kubernetesAuth"][0]["namespaces"] == ["other"]


def test_update_role_leaves_omitted_fields_alone():
    updated = update_kubernetes_auth_role(
        _roles_document("one"), "one", namespaces=["other"]
    )

    assert updated["kubernetesAuth"][0]["serviceAccounts"] == ["vault-reader"]
    assert updated["kubernetesAuth"][0]["access"] == ACCESS


def test_update_role_never_touches_the_name_or_the_cluster():
    """Both identify the role in Vault, so changing either is a delete plus a create."""
    updated = update_kubernetes_auth_role(
        _roles_document("one"), "one", role_description="new"
    )

    assert updated["kubernetesAuth"][0]["name"] == "one"
    assert updated["kubernetesAuth"][0]["cluster"] == "prod-il-1"


def test_update_role_does_not_mutate_its_input():
    original = _roles_document("one")
    update_kubernetes_auth_role(original, "one", role_description="new")

    assert original["kubernetesAuth"][0]["description"] == "CI deployer"


def test_update_of_an_absent_role_raises():
    with pytest.raises(KVStoreNotFound):
        update_kubernetes_auth_role(_roles_document("one"), "nope", ttl="1h")


def test_remove_role_drops_only_the_named_one():
    result = remove_kubernetes_auth_role(_roles_document("one", "two"), "one")

    assert [r["name"] for r in result["kubernetesAuth"]] == ["two"]


def test_remove_of_an_absent_role_raises():
    with pytest.raises(KVStoreNotFound):
        remove_kubernetes_auth_role(_roles_document("one"), "nope")


# --------------------------------------------------------------------------- #
# referrers — the rule a KV delete consults
# --------------------------------------------------------------------------- #
def test_referrers_names_the_roles_that_reach_a_store():
    assert kv_store_referrers(_roles_document("one", "two"), "myapp") == ["one", "two"]


def test_referrers_is_empty_for_an_unreferenced_store():
    assert kv_store_referrers(_roles_document("one"), "billing") == []


@pytest.mark.parametrize("empty", [None, {}, {"kvStores": []}])
def test_referrers_of_a_document_with_no_roles_is_empty(empty):
    assert kv_store_referrers(empty, "myapp") == []


def test_referrers_looks_under_every_capability():
    document = {"kubernetesAuth": [_role(access={"write": ["billing"]})]}

    assert kv_store_referrers(document, "billing") == ["myapp-ci"]


def test_referrers_matches_a_whole_name_not_a_prefix():
    """A substring match would block deleting `app` because `app-two` is referenced."""
    document = {"kubernetesAuth": [_role(access={"read": ["myapp-two"]})]}

    assert kv_store_referrers(document, "myapp") == []


def test_referrers_survives_a_hand_edited_file():
    """A malformed entry must not wedge an unrelated delete."""
    document = {
        "kubernetesAuth": [
            "just a string",
            {"name": "no-access"},
            {"name": "bad-access", "access": "not a mapping"},
            {"name": "bad-stores", "access": {"read": "not a list"}},
            _role(access={"read": ["myapp"]}),
        ]
    }

    assert kv_store_referrers(document, "myapp") == ["myapp-ci"]


# --------------------------------------------------------------------------- #
# the two kinds sharing one file
#
# The sibling-preservation tests above use a synthetic key; these use the real thing in
# both directions, because a create of either kind erasing the other would sail through CI
# looking like a legitimate diff.
# --------------------------------------------------------------------------- #
def _shared_document():
    return add_kubernetes_auth_role(_document("myapp"), _role())


def test_adding_a_role_preserves_the_kv_stores():
    result = add_kubernetes_auth_role(_document("myapp"), _role())

    assert [s["name"] for s in result["kvStores"]] == ["myapp"]
    assert kubernetes_auth_role_names(result) == ["myapp-ci"]


def test_adding_a_store_preserves_the_roles():
    result = add_kv_store(_shared_document(), _store(name="billing"))

    assert [s["name"] for s in result["kvStores"]] == ["myapp", "billing"]
    assert kubernetes_auth_role_names(result) == ["myapp-ci"]


def test_editing_either_kind_preserves_the_other():
    document = _shared_document()

    assert kubernetes_auth_role_names(
        update_kv_store(document, "myapp", description="new")
    ) == ["myapp-ci"]
    assert kv_store_names(
        update_kubernetes_auth_role(document, "myapp-ci", ttl="1h")
    ) == ["myapp"]


def test_removing_either_kind_preserves_the_other():
    document = _shared_document()

    assert kubernetes_auth_role_names(remove_kv_store(document, "myapp")) == ["myapp-ci"]
    assert kv_store_names(remove_kubernetes_auth_role(document, "myapp-ci")) == ["myapp"]


def test_a_shared_file_round_trips_through_yaml():
    document = _shared_document()

    assert yaml.safe_load(render_values_yaml(document)) == document


def test_a_shared_file_renders_both_keys_at_the_top_level():
    rendered = render_values_yaml(_shared_document())

    assert rendered.startswith("kvStores:\n")
    assert "\nkubernetesAuth:\n" in rendered

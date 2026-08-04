"""Naming, the committed document, and the edits applied to it.

These are pure functions, so nothing here imports the library or touches HTTP.

The document is a *list* of stores under `kvStores`, so most of these assert that an
operation touches exactly one entry and leaves its siblings alone.
"""
import pytest
import yaml

from app.helpers import (
    K8sServiceAccountNotFound,
    KVStoreNotFound,
    add_k8s_service_account,
    add_kv_store,
    build_branch_name,
    build_k8s_service_account,
    build_kv_store,
    build_kv_stores_document,
    find_k8s_service_account,
    find_kv_store,
    k8s_service_account_identities,
    k8s_service_account_identity,
    k8s_service_accounts,
    kv_store_names,
    read_kv_stores,
    remove_k8s_service_account,
    remove_kv_store,
    render_values_yaml,
    slugify_mount_path,
    update_kv_store,
    values_file_path,
    yaml_data_equals,
)

ROLES = {"read": ["app01.corp.example.com"]}

# A top-level key nothing in the code knows about — that is the point: the transforms must
# carry it through without understanding it, so a key the pipeline adds later survives an
# edit made by this service.
SIBLING_KEY = "someKeyWeDoNotKnow"
SIBLING_VALUE = [{"name": "whatever", "shape": ["unknown"]}]


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

    assert original[SIBLING_KEY][0]["name"] == "whatever"


def test_reads_ignore_a_sibling_top_level_key():
    """The scans read `kvStores` and nothing else, so a shared file scans correctly."""
    document = _document_with_sibling("one")

    assert kv_store_names(document) == ["one"]
    assert [s["name"] for s in read_kv_stores(document)] == ["one"]
    assert find_kv_store(document, "whatever") is None


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
# Kubernetes service accounts
#
# Nested inside a store, not beside it. These check the *format* — which keys are written,
# what identifies an entry — and the nesting contract: editing a store's bindings must not
# disturb the store's own fields or its siblings.
# --------------------------------------------------------------------------- #
def _account(service_account="vault", namespace="payments", cluster="dev"):
    return build_k8s_service_account(service_account, namespace, cluster)


def _store_with_accounts(name="myapp", accounts=None):
    store = _store(name=name)
    store["roles"]["k8sServiceAccounts"] = list(
        accounts if accounts is not None else [_account()]
    )
    return store


def _bound_document(*names):
    return {"kvStores": [_store_with_accounts(name=n) for n in names]}


def test_build_account_translates_to_the_document_keys():
    """The camelCase key names are the contract with the deploy pipeline."""
    assert build_k8s_service_account("vault", "athena", "dev") == {
        "serviceAccount": "vault",
        "namespace": "athena",
        "cluster": "dev",
    }


def test_build_account_writes_nothing_about_policies():
    """The scope boundary: an entry is a coordinate, never a generated policy."""
    entry = build_k8s_service_account("vault", "athena", "dev")

    assert set(entry) == {"serviceAccount", "namespace", "cluster"}
    for forbidden in ("policies", "policy", "mount", "access", "capabilities", "ttl"):
        assert forbidden not in entry


def test_build_account_keeps_all_three_parts_required():
    """No key is omitted the way an absent cluster used to be — the triple is the identity."""
    entry = build_k8s_service_account("vault", "athena", "dev")

    assert all(entry.values())


def test_accounts_read_out_of_a_store():
    store = _store_with_accounts(accounts=[_account(), _account(namespace="other")])

    assert [a["namespace"] for a in k8s_service_accounts(store)] == ["payments", "other"]


@pytest.mark.parametrize(
    "empty",
    [
        None,
        {},
        _store(),
        {"roles": {}},
        {"roles": {"k8sServiceAccounts": None}},
        {"roles": {"k8sServiceAccounts": []}},
        {"roles": "not a mapping"},
    ],
)
def test_accounts_tolerate_a_store_that_binds_nothing(empty):
    """A store with no bindings simply has no key — what `build_kv_store` writes.

    A missing or malformed `roles` reads as no bindings too, rather than raising: the list
    is reached *through* `roles`, and a hand-edited file must not wedge a scan.
    """
    assert k8s_service_accounts(empty) == []


def test_identity_is_the_triple_in_order():
    assert k8s_service_account_identity(_account()) == ("vault", "payments", "dev")


def test_identity_uses_empty_strings_for_missing_parts():
    """A hand-edited entry missing a key must not raise mid-scan."""
    assert k8s_service_account_identity({"serviceAccount": "vault"}) == ("vault", "", "")


def test_identities_skip_malformed_entries():
    store = _store_with_accounts(accounts=[_account(), "just a string"])

    assert k8s_service_account_identities(store) == [("vault", "payments", "dev")]


def test_find_returns_the_matching_binding():
    store = _store_with_accounts(accounts=[_account(), _account(cluster="prod")])

    assert find_k8s_service_account(store, ("vault", "payments", "prod"))["cluster"] == "prod"


def test_find_returns_none_when_absent():
    assert find_k8s_service_account(_store_with_accounts(), ("nope", "payments", "dev")) is None


def test_find_matches_on_the_whole_triple_not_the_name():
    """Same account, different namespace, is a different binding."""
    store = _store_with_accounts()

    assert find_k8s_service_account(store, ("vault", "other", "dev")) is None


# --------------------------------------------------------------------------- #
# adding a binding
# --------------------------------------------------------------------------- #
def test_add_starts_the_list_on_a_store_that_had_none():
    result = add_k8s_service_account(_document("one"), "one", _account())

    assert result["kvStores"][0]["roles"]["k8sServiceAccounts"] == [_account()]


def test_add_appends_after_the_existing_bindings():
    result = add_k8s_service_account(
        _bound_document("one"), "one", _account(namespace="other")
    )

    assert [
        a["namespace"] for a in result["kvStores"][0]["roles"]["k8sServiceAccounts"]
    ] == ["payments", "other"]


def test_add_leaves_the_stores_own_fields_alone():
    result = add_k8s_service_account(_document("one"), "one", _account())
    store = result["kvStores"][0]

    assert store["name"] == "one"
    assert store["description"] == "payments secrets"
    assert store["roles"]["read"] == ROLES["read"]


def test_add_leaves_sibling_stores_alone():
    result = add_k8s_service_account(_document("one", "two"), "one", _account())

    assert "k8sServiceAccounts" not in result["kvStores"][1]["roles"]


def test_add_does_not_mutate_its_input():
    original = _document("one")
    add_k8s_service_account(original, "one", _account())

    assert "k8sServiceAccounts" not in original["kvStores"][0]["roles"]


def test_add_deep_copies_so_later_edits_do_not_leak():
    account = _account()
    result = add_k8s_service_account(_document("one"), "one", account)
    account["namespace"] = "changed"

    assert (
        result["kvStores"][0]["roles"]["k8sServiceAccounts"][0]["namespace"] == "payments"
    )


def test_add_to_an_absent_store_raises():
    """A binding cannot create the store it lives in — unlike `add_kv_store(None, ...)`."""
    with pytest.raises(KVStoreNotFound):
        add_k8s_service_account(_document("one"), "nope", _account())


# --------------------------------------------------------------------------- #
# removing a binding
# --------------------------------------------------------------------------- #
def test_remove_drops_only_the_matching_binding():
    document = {"kvStores": [_store_with_accounts(accounts=[_account(), _account(cluster="prod")])]}
    result = remove_k8s_service_account(document, "myapp", ("vault", "payments", "dev"))

    assert [
        a["cluster"] for a in result["kvStores"][0]["roles"]["k8sServiceAccounts"]
    ] == ["prod"]


def test_removing_the_last_binding_drops_the_key_entirely():
    """Deliberately unlike `remove_kv_store`, which leaves `kvStores: []` behind.

    A store with no bindings is just a store — exactly what a fresh create writes — so an
    empty list would leave a diff no create would ever produce.
    """
    result = remove_k8s_service_account(
        _bound_document("one"), "one", ("vault", "payments", "dev")
    )

    assert "k8sServiceAccounts" not in result["kvStores"][0]["roles"]
    assert result["kvStores"][0] == _store(name="one")


def test_remove_does_not_mutate_its_input():
    original = _bound_document("one")
    remove_k8s_service_account(original, "one", ("vault", "payments", "dev"))

    assert original["kvStores"][0]["roles"]["k8sServiceAccounts"] == [_account()]


def test_remove_leaves_sibling_stores_alone():
    document = {"kvStores": [_store_with_accounts(name="one"), _store_with_accounts(name="two")]}
    result = remove_k8s_service_account(document, "one", ("vault", "payments", "dev"))

    assert result["kvStores"][1]["roles"]["k8sServiceAccounts"] == [_account()]


def test_remove_of_an_absent_binding_raises():
    with pytest.raises(K8sServiceAccountNotFound):
        remove_k8s_service_account(_bound_document("one"), "one", ("nope", "payments", "dev"))


def test_remove_from_a_store_that_binds_nothing_raises():
    with pytest.raises(K8sServiceAccountNotFound):
        remove_k8s_service_account(_document("one"), "one", ("vault", "payments", "dev"))


def test_remove_from_an_absent_store_raises_store_not_found():
    with pytest.raises(KVStoreNotFound):
        remove_k8s_service_account(_document("one"), "nope", ("vault", "payments", "dev"))


def test_remove_survives_a_hand_edited_list():
    """A malformed neighbour must not wedge an otherwise valid removal."""
    document = {"kvStores": [_store_with_accounts(accounts=[_account(), "junk"])]}
    result = remove_k8s_service_account(document, "myapp", ("vault", "payments", "dev"))

    assert result["kvStores"][0]["roles"]["k8sServiceAccounts"] == ["junk"]


# --------------------------------------------------------------------------- #
# the store and its bindings in one file
#
# The nesting is the whole design: a binding has nowhere to dangle from, so removing a
# store removes its bindings in the same diff.
# --------------------------------------------------------------------------- #
def test_removing_a_store_takes_its_bindings_with_it():
    result = remove_kv_store(_bound_document("one", "two"), "one")

    assert [s["name"] for s in result["kvStores"]] == ["two"]
    assert "one" not in render_values_yaml(result)


def test_editing_only_the_description_leaves_the_bindings_alone():
    result = update_kv_store(_bound_document("one"), "one", description="new")

    assert result["kvStores"][0]["description"] == "new"
    assert result["kvStores"][0]["roles"]["k8sServiceAccounts"] == [_account()]


def test_replacing_roles_wholesale_still_keeps_the_bindings():
    """The trap of nesting under `roles`: a wholesale replace would erase every binding.

    The update body cannot express bindings, so dropping them here would be data loss with
    no way to undo it in the same call — and the erase would look like a legitimate diff.
    """
    result = update_kv_store(
        _bound_document("one"), "one", roles={"read": ["CN=someone-else"]}
    )
    roles = result["kvStores"][0]["roles"]

    assert roles["read"] == ["CN=someone-else"]
    assert "write" not in roles
    assert roles["k8sServiceAccounts"] == [_account()]


def test_replacing_roles_on_an_unbound_store_adds_no_empty_list():
    result = update_kv_store(_document("one"), "one", roles={"read": ["CN=x"]})

    assert "k8sServiceAccounts" not in result["kvStores"][0]["roles"]


def test_the_carried_bindings_stay_last_in_the_mapping():
    """Principals first, then workloads — the order the format spec writes them in."""
    result = update_kv_store(
        _bound_document("one"), "one", roles={"read": ["CN=x"], "write": ["CN=y"]}
    )

    assert list(result["kvStores"][0]["roles"]) == ["read", "write", "k8sServiceAccounts"]


def test_a_no_op_roles_update_on_a_bound_store_compares_equal():
    """Carrying the bindings across must not by itself look like a change."""
    document = _bound_document("one")
    updated = update_kv_store(document, "one", roles=ROLES)

    assert yaml_data_equals(document, updated)


def test_binding_edits_leave_the_stores_principals_alone():
    result = add_k8s_service_account(_bound_document("one"), "one", _account(cluster="prod"))

    assert result["kvStores"][0]["roles"]["read"] == ROLES["read"]


def test_a_bound_store_round_trips_through_yaml():
    document = _bound_document("one")

    assert yaml.safe_load(render_values_yaml(document)) == document


def test_a_bound_store_renders_the_list_inside_the_store():
    """The nesting has to survive serialisation — this is the shape the pipeline reads."""
    parsed = yaml.safe_load(render_values_yaml(_bound_document("one")))

    assert "k8sServiceAccounts" not in parsed
    assert "k8sServiceAccounts" not in parsed["kvStores"][0]
    assert parsed["kvStores"][0]["roles"]["k8sServiceAccounts"] == [_account()]


def test_a_bound_store_renders_the_keys_in_a_readable_order():
    """Name and description first, then roles, then bindings — as the format shows them."""
    rendered = render_values_yaml(_bound_document("one"))

    assert rendered.index("name:") < rendered.index("roles:")
    assert rendered.index("roles:") < rendered.index("k8sServiceAccounts:")


def test_the_bindings_render_one_level_inside_roles():
    """Level with `read`/`write`, not with `roles` — the indentation is the format."""
    lines = render_values_yaml(_bound_document("one")).splitlines()
    read = next(line for line in lines if line.strip().startswith("read:"))
    accounts = next(
        line for line in lines if line.strip().startswith("k8sServiceAccounts:")
    )

    indent = lambda line: len(line) - len(line.lstrip())  # noqa: E731

    assert indent(accounts) == indent(read)

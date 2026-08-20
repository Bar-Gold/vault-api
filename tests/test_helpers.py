"""The document model, pure — no library import, no Bitbucket, no event loop.

What these pin is the *committed artefact*: the exact bytes the deploy pipeline reads. The
format is the pipeline's, so a test here failing means either a deliberate format change
(update the pipeline too) or a regression that would have shipped a file nothing can parse.
"""

import pytest
import yaml

from app.helpers import (
    ACCESS_KEY,
    K8S_ACCESS_FILE,
    OIDC_ACCESS_FILE,
    AccessEntryNotFound,
    DuplicateAccessEntry,
    access_entry_identity,
    access_path_for,
    add_access_entry,
    auth_type_of,
    build_access_document,
    build_branch_name,
    build_k8s_access_entry,
    build_oidc_access_entry,
    find_access_entry,
    k8s_access_path,
    kv_store_dir,
    oidc_access_path,
    partition_access,
    read_access_entries,
    remove_access_entry,
    render_values_yaml,
    replace_access_entries,
    slugify_mount_path,
    tashtiot_access_path,
    yaml_data_equals,
)

DN = "CN=cloudops,OU=Groups,DC=corp,DC=example,DC=com"
OTHER_DN = "CN=platform,OU=Groups,DC=corp,DC=example,DC=com"

# A key this service has never heard of. Every transform must carry it through — see the
# sibling-key tests at the bottom.
SIBLING_KEY = "somethingElseEntirely"


def _oidc(path="*", dn=DN, capabilities=("read", "write")):
    return build_oidc_access_entry(path, dn, list(capabilities))


def _k8s(path="*", name="cloudops-external-secrets", namespace="cloudops", cluster="dev",
         capabilities=("read",)):
    return build_k8s_access_entry(path, name, namespace, cluster, list(capabilities))


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
def test_a_store_is_a_directory_under_its_team():
    assert kv_store_dir("KVs", "cloudops", "my-passwords") == "KVs/cloudops/my-passwords"


def test_the_two_access_files_sit_inside_it():
    assert oidc_access_path("KVs", "cloudops", "my-passwords") == (
        "KVs/cloudops/my-passwords/oidc-access.yaml"
    )
    assert k8s_access_path("KVs", "cloudops", "my-passwords") == (
        "KVs/cloudops/my-passwords/k8s-access.yaml"
    )


@pytest.mark.parametrize(
    "auth_type,expected",
    [("oidc", OIDC_ACCESS_FILE), ("k8s", K8S_ACCESS_FILE)],
)
def test_the_auth_type_picks_the_file(auth_type, expected):
    """The one mapping from kind to filename, so no caller names a file itself."""
    assert access_path_for("KVs", "cloudops", "my-passwords", auth_type).endswith(expected)


def test_an_unknown_auth_type_falls_back_to_oidc():
    """A hand-edited entry with a typo'd kind reads as the default rather than crashing."""
    assert access_path_for("KVs", "t", "k", "nonsense").endswith(OIDC_ACCESS_FILE)


def test_the_tashtiot_file_is_flat_not_a_directory():
    """The second layout: one file per team, no store directory and no oidc/k8s split."""
    assert tashtiot_access_path("KVs", "Tashtiot", "cloudops") == "KVs/Tashtiot/cloudops.yaml"


@pytest.mark.parametrize("values_dir", ["KVs", "/KVs", "KVs/"])
def test_stray_slashes_do_not_double_up(values_dir):
    assert kv_store_dir(values_dir, "cloudops", "my-passwords") == "KVs/cloudops/my-passwords"


# --------------------------------------------------------------------------- #
# branch names
# --------------------------------------------------------------------------- #
def test_branch_name_carries_team_and_store():
    assert (
        build_branch_name("cloudops", "my-passwords", "ab12cd34", "vault-kv")
        == "vault-kv/cloudops-my-passwords-ab12cd34"
    )


def test_branch_name_names_the_store_once_when_they_match():
    assert (
        build_branch_name("cloudops", "cloudops", "ab12", "vault-kv")
        == "vault-kv/cloudops-ab12"
    )


def test_branch_name_has_exactly_one_slash():
    """More would nest the ref, and git cannot hold both a/b and a/b/c."""
    assert build_branch_name("cloudops", "my-passwords", "ab12", "vault-kv").count("/") == 1


def test_slugify_flattens_slashes():
    assert slugify_mount_path("/a/b/") == "a-b"


# --------------------------------------------------------------------------- #
# the committed text
# --------------------------------------------------------------------------- #
def test_oidc_file_has_the_expected_shape():
    """Byte-for-byte: this text is the contract with the deploy pipeline.

    Note the sequence indentation — items sit **under** their key, not flush with it.
    PyYAML's default is the flush form, which parses identically but is not the format the
    pipeline expects, so `_BlockStyleDumper.increase_indent` forces this shape.
    """
    rendered = render_values_yaml(build_access_document([_oidc()]))

    assert rendered == (
        "access:\n"
        "  - path: '*'\n"
        "    authType: oidc\n"
        f"    dn: {DN}\n"
        "    capabilities:\n"
        "      - read\n"
        "      - write\n"
    )


def test_k8s_file_has_the_expected_shape():
    rendered = render_values_yaml(build_access_document([_k8s()]))

    assert rendered == (
        "access:\n"
        "  - path: '*'\n"
        "    authType: k8s\n"
        "    name: cloudops-external-secrets\n"
        "    namespace: cloudops\n"
        "    cluster: dev\n"
        "    capabilities:\n"
        "      - read\n"
    )


def test_the_star_path_is_quoted():
    """A bare `*` is a YAML alias, so an unquoted one would not round-trip."""
    rendered = render_values_yaml(build_access_document([_oidc(path="*")]))

    assert "path: '*'" in rendered
    assert yaml.safe_load(rendered)["access"][0]["path"] == "*"


def test_a_scoped_path_round_trips():
    rendered = render_values_yaml(build_access_document([_oidc(path="services/+/tyk/*")]))

    assert yaml.safe_load(rendered)["access"][0]["path"] == "services/+/tyk/*"


def test_field_order_is_the_specs_order():
    """`sort_keys=False` is what preserves it; alphabetical would reorder every entry."""
    entry = _k8s()

    assert list(entry) == ["path", "authType", "name", "namespace", "cluster", "capabilities"]


def test_rendered_yaml_round_trips():
    document = build_access_document([_oidc(), _k8s()])

    assert yaml.safe_load(render_values_yaml(document)) == document


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def test_reading_an_absent_key_is_an_empty_list():
    assert read_access_entries({}) == []


def test_reading_a_null_access_key_is_an_empty_list():
    """`access:` with nothing under it parses to None — an empty file, not a corrupt one."""
    assert read_access_entries(yaml.safe_load("access:\n")) == []


def test_reading_a_non_dict_is_an_empty_list():
    assert read_access_entries(None) == []
    assert read_access_entries("not a document") == []


def test_non_dict_entries_are_skipped():
    """A hand-edited list cannot wedge an unrelated edit."""
    assert read_access_entries({ACCESS_KEY: [_oidc(), "junk", 42]}) == [_oidc()]


def test_auth_type_defaults_to_oidc_when_absent():
    """The format's own default: an entry with a dn and no authType is an OIDC grant."""
    assert auth_type_of({"path": "*", "dn": DN}) == "oidc"


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #
def test_capabilities_are_not_part_of_identity():
    """Granting read, then read+write, to the same principal is an edit of one entry."""
    assert access_entry_identity(_oidc(capabilities=["read"])) == access_entry_identity(
        _oidc(capabilities=["read", "write"])
    )


def test_the_dn_is_part_of_an_oidc_identity():
    assert access_entry_identity(_oidc(dn=DN)) != access_entry_identity(_oidc(dn=OTHER_DN))


def test_the_path_is_part_of_identity():
    """The same group on two paths is two grants, which is the intended shape."""
    assert access_entry_identity(_oidc(path="*")) != access_entry_identity(
        _oidc(path="services/*")
    )


@pytest.mark.parametrize("field,value", [
    ("name", "other"), ("namespace", "other"), ("cluster", "other"),
])
def test_all_three_k8s_scalars_are_part_of_identity(field, value):
    """The same account in two namespaces or two clusters is two principals."""
    assert access_entry_identity(_k8s()) != access_entry_identity(_k8s(**{field: value}))


def test_the_two_kinds_never_share_an_identity():
    assert access_entry_identity(_oidc()) != access_entry_identity(_k8s())


def test_identity_tolerates_missing_keys():
    """A hand-edited entry cannot wedge an unrelated removal."""
    assert access_entry_identity({"authType": "oidc"}) == ("", "oidc", "")


# --------------------------------------------------------------------------- #
# partitioning
# --------------------------------------------------------------------------- #
def test_partition_splits_by_auth_type():
    oidc, k8s = partition_access([_oidc(), _k8s(), _oidc(dn=OTHER_DN)])

    assert [entry["dn"] for entry in oidc] == [DN, OTHER_DN]
    assert [entry["name"] for entry in k8s] == ["cloudops-external-secrets"]


def test_partition_preserves_order_within_a_kind():
    oidc, _ = partition_access([_oidc(path="b"), _oidc(path="a")])

    assert [entry["path"] for entry in oidc] == ["b", "a"]


def test_partition_of_one_kind_leaves_the_other_empty():
    """What makes a store with no Kubernetes grants have no k8s-access.yaml at all."""
    oidc, k8s = partition_access([_oidc()])

    assert oidc and k8s == []


# --------------------------------------------------------------------------- #
# transforms
# --------------------------------------------------------------------------- #
def test_add_appends_to_the_end():
    document = build_access_document([_oidc()])

    updated = add_access_entry(document, _oidc(dn=OTHER_DN))

    assert [entry["dn"] for entry in updated[ACCESS_KEY]] == [DN, OTHER_DN]


def test_add_accepts_none_so_a_first_grant_creates_the_file():
    assert add_access_entry(None, _oidc())[ACCESS_KEY] == [_oidc()]


def test_add_refuses_a_duplicate_identity():
    document = build_access_document([_oidc(capabilities=["read"])])

    with pytest.raises(DuplicateAccessEntry):
        add_access_entry(document, _oidc(capabilities=["read", "write"]))


def test_add_does_not_mutate_its_input():
    """What lets an update diff old against new and skip a no-op commit."""
    document = build_access_document([_oidc()])

    add_access_entry(document, _k8s())

    assert len(document[ACCESS_KEY]) == 1


def test_remove_drops_the_named_entry():
    document = build_access_document([_oidc(), _oidc(dn=OTHER_DN)])

    updated = remove_access_entry(document, access_entry_identity(_oidc()))

    assert [entry["dn"] for entry in updated[ACCESS_KEY]] == [OTHER_DN]


def test_remove_ignores_capabilities_when_matching():
    """The caller revoking a grant does not have to know what it currently permits."""
    document = build_access_document([_oidc(capabilities=["read", "write"])])

    updated = remove_access_entry(
        document, access_entry_identity(_oidc(capabilities=["read"]))
    )

    assert updated[ACCESS_KEY] == []


def test_remove_raises_when_it_is_not_there():
    with pytest.raises(AccessEntryNotFound):
        remove_access_entry(build_access_document([_oidc()]), access_entry_identity(_k8s()))


def test_remove_keeps_an_emptied_list():
    """Unlike a store delete: the caller decides separately whether to drop the file."""
    updated = remove_access_entry(
        build_access_document([_oidc()]), access_entry_identity(_oidc())
    )

    assert updated == {ACCESS_KEY: []}


def test_remove_does_not_mutate_its_input():
    document = build_access_document([_oidc()])

    remove_access_entry(document, access_entry_identity(_oidc()))

    assert len(document[ACCESS_KEY]) == 1


def test_replace_swaps_the_whole_list():
    document = build_access_document([_oidc(), _k8s()])

    updated = replace_access_entries(document, [_oidc(dn=OTHER_DN)])

    assert updated[ACCESS_KEY] == [_oidc(dn=OTHER_DN)]


def test_find_returns_the_entry_or_none():
    document = build_access_document([_oidc()])

    assert find_access_entry(document, access_entry_identity(_oidc())) == _oidc()
    assert find_access_entry(document, access_entry_identity(_k8s())) is None


# --------------------------------------------------------------------------- #
# sibling keys
#
# Every transform deep-copies the whole document and replaces only `access`, so a key this
# service has never heard of survives. That is not hypothetical tidiness: an earlier
# revision rebuilt the document from its own list alone, which silently dropped every other
# key — and the erase would have looked like a legitimate diff in the pull request.
# --------------------------------------------------------------------------- #
def _with_sibling(entries):
    return {ACCESS_KEY: list(entries), SIBLING_KEY: {"kept": ["untouched"]}}


def test_add_keeps_a_sibling_key():
    updated = add_access_entry(_with_sibling([_oidc()]), _k8s())

    assert updated[SIBLING_KEY] == {"kept": ["untouched"]}


def test_remove_keeps_a_sibling_key():
    updated = remove_access_entry(
        _with_sibling([_oidc()]), access_entry_identity(_oidc())
    )

    assert updated[SIBLING_KEY] == {"kept": ["untouched"]}


def test_replace_keeps_a_sibling_key():
    updated = replace_access_entries(_with_sibling([_oidc()]), [_k8s()])

    assert updated[SIBLING_KEY] == {"kept": ["untouched"]}


def test_a_sibling_key_survives_a_render_round_trip():
    rendered = render_values_yaml(add_access_entry(_with_sibling([_oidc()]), _k8s()))

    assert yaml.safe_load(rendered)[SIBLING_KEY] == {"kept": ["untouched"]}


# --------------------------------------------------------------------------- #
# comparison
# --------------------------------------------------------------------------- #
def test_yaml_data_equals_ignores_order():
    assert yaml_data_equals({"a": [1, 2]}, {"a": [2, 1]})


def test_yaml_data_equals_accepts_text():
    assert yaml_data_equals("access:\n  - path: '*'\n", {"access": [{"path": "*"}]})


def test_yaml_data_equals_sees_a_real_change():
    assert not yaml_data_equals(
        build_access_document([_oidc(capabilities=["read"])]),
        build_access_document([_oidc(capabilities=["read", "write"])]),
    )

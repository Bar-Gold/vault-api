"""Request validation — the cheapest place to stop a bad grant.

The request mirrors the committed document, so these are also the rules the *file* obeys:
anything that passes here is written verbatim.
"""

import re

import pytest
from pydantic import ValidationError

from app.v1.vault.schemas import (
    KV_NAME_PATTERN,
    TEAM_PATTERN,
    VAULT_PATH_PATTERN,
    Capability,
    K8sAccessEntry,
    OidcAccessEntry,
    TashtiotAccessUpsert,
    VaultKVCreate,
    VaultKVUpdate,
)

DN = "CN=cloudops,OU=Groups,DC=corp,DC=example,DC=com"

OIDC = {"path": "*", "auth_type": "oidc", "dn": DN, "capabilities": ["read"]}
K8S = {
    "path": "*",
    "auth_type": "k8s",
    "name": "cloudops-external-secrets",
    "namespace": "cloudops",
    "cluster": "dev",
    "capabilities": ["read", "write"],
}


def _create(**overrides):
    values = {"team": "cloudops", "kv_name": "my-passwords", "access": [OIDC]}
    values.update(overrides)
    return VaultKVCreate(**values)


# --------------------------------------------------------------------------- #
# the request's shape
# --------------------------------------------------------------------------- #
def test_create_takes_exactly_three_fields():
    """No kv_description: the format defines no home for it."""
    assert set(VaultKVCreate.model_fields) == {"team", "kv_name", "access"}


def test_there_is_no_description_field():
    with pytest.raises(ValidationError):
        _create(kv_description="payments secrets")


def test_there_is_no_roles_field():
    """The previous format's shape. Sending it must fail loudly, not fall through."""
    with pytest.raises(ValidationError):
        _create(roles={"read": [DN]})


def test_an_unknown_field_is_rejected():
    """Ignoring it would answer 201 for a request nobody meant to send."""
    with pytest.raises(ValidationError):
        _create(environment="prod")


def test_a_mistyped_field_is_rejected():
    with pytest.raises(ValidationError):
        _create(tea="cloudops")


# --------------------------------------------------------------------------- #
# team and kv_name — the two path segments
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", ["cloudops", "my-team", "team-a1", "a1"])
def test_valid_names(value):
    assert _create(team=value, kv_name=value).team == value


@pytest.mark.parametrize("value", [
    "CloudOps",     # uppercase
    "my_team",      # underscore
    "-lead",
    "trail-",
    "my--team",
    "my team",
    "",
    # Both segments land in the committed path, so traversal must not pass.
    "../etc/passwd",
    "a/b",
    "/cloudops",
    "cloudops/",
    "..",
    ".",
])
def test_rejected_names(value):
    with pytest.raises(ValidationError):
        _create(team=value)
    with pytest.raises(ValidationError):
        _create(kv_name=value)


def test_both_segments_share_one_shape():
    """Which is why either can be a directory segment without a second rule."""
    assert TEAM_PATTERN == KV_NAME_PATTERN


# --------------------------------------------------------------------------- #
# path — a Vault pattern, not a filesystem one
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [
    "*",
    "services/+/tyk/*",
    "+/cloudops/*",
    "app/config",
    "a.b/c-d_e",
])
def test_valid_paths(path):
    assert _create(access=[{**OIDC, "path": path}]).access[0].path == path


@pytest.mark.parametrize("path", ["", "/leading", "../escape", "a/../b", "has space", "a|b"])
def test_rejected_paths(path):
    with pytest.raises(ValidationError):
        _create(access=[{**OIDC, "path": path}])


def test_the_path_pattern_allows_slashes_unlike_the_name_patterns():
    """A Vault path never reaches a repo path, so it can be permissive about them."""
    assert re.fullmatch(VAULT_PATH_PATTERN, "services/+/tyk/*")
    assert not re.fullmatch(KV_NAME_PATTERN, "services/tyk")


# --------------------------------------------------------------------------- #
# capabilities
# --------------------------------------------------------------------------- #
def test_the_capability_set_is_the_single_place_to_change():
    assert {c.value for c in Capability} == {"read", "write"}


@pytest.mark.parametrize("capabilities", [["read"], ["write"], ["read", "write"]])
def test_valid_capabilities(capabilities):
    entry = _create(access=[{**OIDC, "capabilities": capabilities}]).access[0]
    assert [c.value for c in entry.capabilities] == capabilities


def test_an_empty_capability_list_is_rejected():
    """An entry granting nothing is not a grant."""
    with pytest.raises(ValidationError):
        _create(access=[{**OIDC, "capabilities": []}])


def test_a_repeated_capability_is_rejected():
    with pytest.raises(ValidationError):
        _create(access=[{**OIDC, "capabilities": ["read", "read"]}])


def test_an_unknown_capability_is_rejected():
    """Closed set, unlike the DN beside it: these reach the pipeline as Vault policy."""
    with pytest.raises(ValidationError):
        _create(access=[{**OIDC, "capabilities": ["delete"]}])


# --------------------------------------------------------------------------- #
# the discriminator
# --------------------------------------------------------------------------- #
def test_auth_type_selects_the_shape():
    payload = _create(access=[OIDC, K8S])

    assert isinstance(payload.access[0], OidcAccessEntry)
    assert isinstance(payload.access[1], K8sAccessEntry)


def test_an_entry_with_no_auth_type_is_rejected():
    """The two shapes carry different required fields; guessing writes the wrong file."""
    with pytest.raises(ValidationError):
        _create(access=[{"path": "*", "dn": DN, "capabilities": ["read"]}])


def test_an_unknown_auth_type_is_rejected():
    with pytest.raises(ValidationError):
        _create(access=[{**OIDC, "auth_type": "ldap"}])


def test_an_oidc_entry_may_not_carry_k8s_fields():
    with pytest.raises(ValidationError):
        _create(access=[{**OIDC, "namespace": "cloudops"}])


def test_a_k8s_entry_may_not_carry_a_dn():
    with pytest.raises(ValidationError):
        _create(access=[{**K8S, "dn": DN}])


# --------------------------------------------------------------------------- #
# oidc entries
# --------------------------------------------------------------------------- #
def test_a_blank_dn_is_rejected():
    with pytest.raises(ValidationError):
        _create(access=[{**OIDC, "dn": "   "}])


def test_a_dn_is_stripped():
    assert _create(access=[{**OIDC, "dn": f"  {DN}  "}]).access[0].dn == DN


def test_a_dn_is_otherwise_unvalidated():
    """The vocabulary is the directory's, not ours — any pattern tight enough is wrong."""
    assert _create(access=[{**OIDC, "dn": "anything at all"}]).access[0].dn


# --------------------------------------------------------------------------- #
# k8s entries — Kubernetes' rules, not ours
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["vault", "a-b", "a.b.c", "a1"])
def test_valid_service_account_names(name):
    assert _create(access=[{**K8S, "name": name}]).access[0].name == name


@pytest.mark.parametrize("name", ["Vault", "under_score", "-lead", "*", "a b"])
def test_rejected_service_account_names(name):
    with pytest.raises(ValidationError):
        _create(access=[{**K8S, "name": name}])


def test_a_namespace_may_not_contain_a_dot():
    """A namespace is an RFC 1123 *label*; the account name is a *subdomain*."""
    assert _create(access=[{**K8S, "name": "a.b"}]).access[0].name == "a.b"
    with pytest.raises(ValidationError):
        _create(access=[{**K8S, "namespace": "a.b"}])


@pytest.mark.parametrize("field", ["name", "namespace"])
def test_a_wildcard_principal_is_rejected(field):
    """It would grant every workload in the cluster — that needs a human, not an API call."""
    with pytest.raises(ValidationError):
        _create(access=[{**K8S, field: "*"}])


def test_a_long_namespace_is_rejected():
    with pytest.raises(ValidationError):
        _create(access=[{**K8S, "namespace": "a" * 64}])


@pytest.mark.parametrize("field", ["name", "namespace", "cluster"])
def test_all_three_k8s_fields_are_required(field):
    entry = {key: value for key, value in K8S.items() if key != field}
    with pytest.raises(ValidationError):
        _create(access=[entry])


# --------------------------------------------------------------------------- #
# the access list as a whole
# --------------------------------------------------------------------------- #
def test_an_empty_access_list_is_rejected():
    with pytest.raises(ValidationError):
        _create(access=[])


def test_access_is_required():
    with pytest.raises(ValidationError):
        VaultKVCreate(team="cloudops", kv_name="my-passwords")


def test_two_grants_with_the_same_identity_are_rejected():
    """The same principal on the same path twice is a contradiction, not an addition."""
    with pytest.raises(ValidationError):
        _create(access=[OIDC, {**OIDC, "capabilities": ["write"]}])


def test_the_same_dn_on_two_paths_is_fine():
    """That is the intended shape — one entry per path."""
    assert len(_create(access=[OIDC, {**OIDC, "path": "services/*"}]).access) == 2


def test_the_same_account_in_two_namespaces_is_fine():
    assert len(_create(access=[K8S, {**K8S, "namespace": "other"}]).access) == 2


# --------------------------------------------------------------------------- #
# the other request models share the rules
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("model", [VaultKVUpdate, TashtiotAccessUpsert])
def test_the_list_models_reject_an_empty_list(model):
    with pytest.raises(ValidationError):
        model(access=[])


@pytest.mark.parametrize("model", [VaultKVUpdate, TashtiotAccessUpsert])
def test_the_list_models_forbid_extras(model):
    with pytest.raises(ValidationError):
        model(access=[OIDC], team="cloudops")


def test_update_does_not_take_a_name():
    """Renaming is a Vault migration, not a field edit — a delete plus a create."""
    assert set(VaultKVUpdate.model_fields) == {"access"}


def test_a_single_grant_is_the_body_on_its_own():
    """The sub-resource form posts one entry, with no wrapper object around it."""
    assert K8sAccessEntry(**K8S).name == "cloudops-external-secrets"


def test_a_single_grant_forbids_extras():
    with pytest.raises(ValidationError):
        OidcAccessEntry(**OIDC, kv_name="my-passwords")

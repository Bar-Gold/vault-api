"""Request validation — the cheapest place to stop a bad grant.

The request mirrors the committed document, so these are also the rules the *file* obeys:
anything that passes here is written verbatim.
"""

import re

import pytest
from pydantic import ValidationError

from app.v1.vault.schemas import (
    KV_NAME_PATTERN,
    METADATA_MAX_LENGTH,
    TEAM_PATTERN,
    VAULT_PATH_PATTERN,
    Capability,
    DeleteRequest,
    K8sAccessEntry,
    K8sAccessEntryRequest,
    K8sAccessRevoke,
    OidcAccessEntry,
    OidcAccessEntryRequest,
    OidcAccessRevoke,
    RequestMetadata,
    TashtiotAccessUpsert,
    VaultKVCreate,
    VaultKVUpdate,
)
from tests.v1.vault.conftest import METADATA

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
    values = {
        "team": "cloudops",
        "kv_name": "my-passwords",
        "access": [OIDC],
        "metadata": METADATA,
    }
    values.update(overrides)
    return VaultKVCreate(**values)


# --------------------------------------------------------------------------- #
# the request's shape
# --------------------------------------------------------------------------- #
def test_create_takes_exactly_four_fields():
    """No kv_description: the format defines no home for it.

    And `metadata`, which is required but is the one field that never reaches a file.
    """
    assert set(VaultKVCreate.model_fields) == {"team", "kv_name", "access", "metadata"}


def test_there_is_no_description_field():
    with pytest.raises(ValidationError):
        _create(kv_description="payments secrets")


def test_there_is_no_roles_field():
    """The previous format's shape. Sending it must fail loudly, not fall through."""
    with pytest.raises(ValidationError):
        _create(roles={"read": [DN]})


def test_an_unknown_field_is_rejected():
    """Ignoring it would answer 201 for a request nobody meant to send.

    A *top-level* `environment` is still forbidden. This is not a duplicate of the
    metadata tests: the block's own `environment` is a different, nested key.
    """
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
    """`metadata` is passed so the failure is attributable to `access` alone."""
    with pytest.raises(ValidationError):
        VaultKVCreate(team="cloudops", kv_name="my-passwords", metadata=METADATA)


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
        model(access=[], metadata=METADATA)


@pytest.mark.parametrize("model", [VaultKVUpdate, TashtiotAccessUpsert])
def test_the_list_models_forbid_extras(model):
    with pytest.raises(ValidationError):
        model(access=[OIDC], metadata=METADATA, team="cloudops")


def test_update_does_not_take_a_name():
    """Renaming is a Vault migration, not a field edit — a delete plus a create."""
    assert set(VaultKVUpdate.model_fields) == {"access", "metadata"}


def test_a_single_grant_is_the_body_on_its_own():
    """The sub-resource form posts one entry, with no wrapper object around it.

    The body is the *Request* entry — the same grant plus the metadata block — because
    metadata is a property of the request rather than of a grant.
    """
    entry = K8sAccessEntryRequest(**K8S, metadata=METADATA)

    assert entry.name == "cloudops-external-secrets"
    assert entry.metadata.project == "cloudops"


def test_a_single_grant_forbids_extras():
    with pytest.raises(ValidationError):
        OidcAccessEntryRequest(**OIDC, metadata=METADATA, kv_name="my-passwords")


# --------------------------------------------------------------------------- #
# metadata — required on every body, and the one field that never reaches a file
# --------------------------------------------------------------------------- #
METADATA_NAMES = ("project", "network", "region", "space", "environment", "island")


def _body_without_metadata(model):
    """The smallest valid body for each body model, minus the metadata block."""
    if model is VaultKVCreate:
        return {"team": "cloudops", "kv_name": "my-passwords", "access": [OIDC]}
    if model in (VaultKVUpdate, TashtiotAccessUpsert):
        return {"access": [OIDC]}
    if model is OidcAccessEntryRequest:
        return dict(OIDC)
    if model is OidcAccessRevoke:
        return _identity(OIDC)
    if model is K8sAccessRevoke:
        return _identity(K8S)
    if model is DeleteRequest:
        return {}
    return dict(K8S)


def _identity(entry):
    """A grant minus its capabilities: what a revoke body names."""
    return {key: value for key, value in entry.items() if key != "capabilities"}


@pytest.mark.parametrize("model", [
    VaultKVCreate,
    VaultKVUpdate,
    TashtiotAccessUpsert,
    OidcAccessEntryRequest,
    K8sAccessEntryRequest,
    OidcAccessRevoke,
    K8sAccessRevoke,
    DeleteRequest,
])
def test_metadata_is_required_on_every_body_model(model):
    """Every body, one rule — an omitted block is a 422, never a default."""
    with pytest.raises(ValidationError):
        model(**_body_without_metadata(model))


@pytest.mark.parametrize("name", METADATA_NAMES)
def test_metadata_needs_all_six_fields(name):
    """An incomplete block is a 422: five coordinates do not locate a deployment."""
    block = {key: value for key, value in METADATA.items() if key != name}

    with pytest.raises(ValidationError):
        _create(metadata=block)


@pytest.mark.parametrize("name", METADATA_NAMES)
@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_metadata_value_is_rejected(name, value):
    with pytest.raises(ValidationError):
        _create(metadata={**METADATA, name: value})


@pytest.mark.parametrize("name", METADATA_NAMES)
def test_metadata_values_are_stripped(name):
    """The same rule `dn` follows, so the description reads without stray spaces."""
    payload = _create(metadata={**METADATA, name: " alpha "})

    assert getattr(payload.metadata, name) == "alpha"


def test_metadata_is_capped_at_253():
    """Same number as the service-account limit, deliberately not the same rule."""
    assert _create(metadata={**METADATA, "project": "a" * METADATA_MAX_LENGTH})

    with pytest.raises(ValidationError):
        _create(metadata={**METADATA, "project": "a" * (METADATA_MAX_LENGTH + 1)})


def test_metadata_forbids_an_unknown_field():
    """A typo'd seventh key is a 422, not a silently dropped coordinate."""
    with pytest.raises(ValidationError):
        _create(metadata={**METADATA, "zone": "il-central-1a"})


@pytest.mark.parametrize("value", [
    "with spaces",
    "dotted.value",
    "a/b/c",
    "UPPERCASE",
    "מרכז",
    "island-a_1",
])
def test_metadata_values_are_free_form(value):
    """The vocabulary is the deploying team's, exactly like `dn` — no pattern here.

    Anything tight enough to describe the organisation's names would reject forms this
    service does not own, so only blank and over-long are refused.
    """
    for name in METADATA_NAMES:
        assert getattr(_create(metadata={**METADATA, name: value}).metadata, name) == value


def test_a_grant_inside_a_list_may_not_carry_metadata():
    """Metadata is a property of the *request*, not of a grant."""
    with pytest.raises(ValidationError):
        _create(access=[{**OIDC, "metadata": METADATA}])


@pytest.mark.parametrize("request_model,base_model", [
    (OidcAccessEntryRequest, OidcAccessEntry),
    (K8sAccessEntryRequest, K8sAccessEntry),
])
def test_the_request_entry_is_the_plain_entry_plus_metadata(request_model, base_model):
    assert set(request_model.model_fields) == set(base_model.model_fields) | {"metadata"}


def test_a_request_entry_is_still_the_entry_it_extends():
    """What keeps `to_document_entry`'s dispatch correct.

    It branches on `isinstance(entry, K8sAccessEntry)`, so a later "let us stop
    subclassing" refactor would silently route every k8s grant to the OIDC builder.
    """
    oidc = OidcAccessEntryRequest(**OIDC, metadata=METADATA)
    k8s = K8sAccessEntryRequest(**K8S, metadata=METADATA)

    assert isinstance(oidc, OidcAccessEntry)
    assert isinstance(k8s, K8sAccessEntry)
    # ...and not the other one, which is the half the dispatch actually reads.
    assert not isinstance(oidc, K8sAccessEntry)


@pytest.mark.parametrize("model,values", [
    (OidcAccessEntryRequest, OIDC),
    (K8sAccessEntryRequest, K8S),
])
def test_the_request_entries_forbid_extras(model, values):
    """The config is merged with the parent's, so `extra: forbid` travels along."""
    with pytest.raises(ValidationError):
        model(**values, metadata=METADATA, kv_name="x")


def test_the_metadata_log_line_keeps_declaration_order():
    """`routes.py` logs this; two requests must read the same way."""
    text = RequestMetadata(**METADATA).as_log_text()

    assert text == " ".join(f"{name}={METADATA[name]}" for name in METADATA_NAMES)


# --------------------------------------------------------------------------- #
# the DELETE bodies — a revoke names a grant's identity, a delete names nothing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("model,entry", [(OidcAccessRevoke, OIDC), (K8sAccessRevoke, K8S)])
def test_a_revoke_is_the_identity_plus_metadata(model, entry):
    revoke = model(**_identity(entry), metadata=METADATA)

    assert revoke.path == "*"
    assert revoke.metadata.project == "cloudops"


@pytest.mark.parametrize("model,entry", [(OidcAccessRevoke, OIDC), (K8sAccessRevoke, K8S)])
def test_a_revoke_refuses_capabilities(model, entry):
    """Capabilities are not part of identity; taking them would suggest a partial revoke."""
    with pytest.raises(ValidationError):
        model(**entry, metadata=METADATA)


@pytest.mark.parametrize("missing", ["name", "namespace", "cluster", "path"])
def test_a_k8s_revoke_needs_its_whole_identity(missing):
    """The same account in two namespaces or two clusters is two principals."""
    values = {key: value for key, value in _identity(K8S).items() if key != missing}

    with pytest.raises(ValidationError):
        K8sAccessRevoke(**values, metadata=METADATA)


def test_an_oidc_revoke_refuses_a_k8s_field():
    """A mixed identity would resolve to *some* entry; refusing it is the safe answer."""
    with pytest.raises(ValidationError):
        OidcAccessRevoke(**_identity(OIDC), namespace="cloudops", metadata=METADATA)


def test_a_revoke_dn_is_stripped_like_a_grant_dn():
    """Shared validator: a DN sent with a stray space names the grant it was added as."""
    revoke = OidcAccessRevoke(**{**_identity(OIDC), "dn": f"  {DN} "}, metadata=METADATA)

    assert revoke.dn == DN


def test_a_revoke_path_obeys_the_grant_path_rules():
    """`path` and its validator come from the same base as the entries'."""
    with pytest.raises(ValidationError):
        OidcAccessRevoke(**{**_identity(OIDC), "path": "a/../b"}, metadata=METADATA)


def test_a_delete_body_is_the_metadata_alone():
    assert set(DeleteRequest.model_fields) == {"metadata"}

    with pytest.raises(ValidationError):
        DeleteRequest(metadata=METADATA, kv_name="my-passwords")

"""Request validation for Kubernetes auth roles — the cheapest place to stop a bad grant.

Two things here are not style rules. `*` is rejected because it binds every workload in a
cluster, and the name patterns are Kubernetes' own, not ours: a name this service accepted
but the cluster rejects would fail at admission, long after the pull request merged.
"""
import pytest
from pydantic import ValidationError

from app.v1.vault.schemas import (
    ALLOWED_KV_ACCESS_KEYS,
    ALLOWED_ROLE_KEYS,
    FQDN_PATTERN,
    K8S_NAMESPACE_PATTERN,
    K8S_SERVICE_ACCOUNT_PATTERN,
    VaultKubernetesAuthCreate,
    VaultKubernetesAuthUpdate,
)

ACCESS = {"read": ["myapp"]}


def _create(**overrides):
    values = {
        "file": "payments",
        "role_name": "myapp-ci",
        "role_description": "CI deployer for the payments app",
        "cluster": "prod-il-1",
        "service_accounts": ["vault-reader"],
        "namespaces": ["payments"],
        "access": ACCESS,
    }
    values.update(overrides)
    return VaultKubernetesAuthCreate(**values)


# --------------------------------------------------------------------------- #
# role_name — addressed as {file}/{role_name}, so single-segment
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role_name", ["myapp-ci", "ci", "a1", "my-app-ci"])
def test_valid_role_names(role_name):
    assert _create(role_name=role_name).role_name == role_name


@pytest.mark.parametrize(
    "role_name",
    ["MyApp", "my_app", "-ci", "ci-", "my--ci", "my ci", "", "payments/ci", ".."],
)
def test_rejected_role_names(role_name):
    with pytest.raises(ValidationError):
        _create(role_name=role_name)


def test_role_name_length_is_capped():
    with pytest.raises(ValidationError):
        _create(role_name="a" * 129)


# --------------------------------------------------------------------------- #
# cluster — optional, because an estate with one auth mount has none to name
# --------------------------------------------------------------------------- #
def test_cluster_may_be_omitted():
    """Forcing a caller to invent a value is worse than allowing its absence."""
    assert _create(cluster=None).cluster is None


def test_cluster_is_absent_by_default():
    payload = VaultKubernetesAuthCreate(
        file="payments",
        role_name="myapp-ci",
        role_description="d",
        service_accounts=["vault-reader"],
        namespaces=["payments"],
        access=ACCESS,
    )

    assert payload.cluster is None


@pytest.mark.parametrize("cluster", ["Prod", "prod_il", "prod/il", "-prod"])
def test_rejected_clusters(cluster):
    with pytest.raises(ValidationError):
        _create(cluster=cluster)


# --------------------------------------------------------------------------- #
# service accounts and namespaces — RFC 1123, and no wildcards
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "accounts",
    [["vault-reader"], ["a"], ["reader", "writer"], ["sa.with.dots"], ["a1-b2"]],
)
def test_valid_service_accounts(accounts):
    assert _create(service_accounts=accounts).service_accounts == accounts


@pytest.mark.parametrize(
    "accounts",
    [
        [],  # a role nothing can assume is not useful
        ["Reader"],  # uppercase: Kubernetes rejects it at admission
        ["-reader"],
        ["reader-"],
        ["read_er"],
        ["  "],
        ["reader", ""],
        ["dup", "dup"],
        ["a" * 254],  # RFC 1123 subdomain cap
    ],
)
def test_rejected_service_accounts(accounts):
    with pytest.raises(ValidationError):
        _create(service_accounts=accounts)


@pytest.mark.parametrize("namespaces", [["payments"], ["a"], ["ns-1", "ns-2"]])
def test_valid_namespaces(namespaces):
    assert _create(namespaces=namespaces).namespaces == namespaces


@pytest.mark.parametrize(
    "namespaces",
    [
        [],
        ["Payments"],
        ["ns.with.dots"],  # a namespace is a *label*: no dots
        ["-ns"],
        ["ns_1"],
        ["dup", "dup"],
        ["a" * 64],  # RFC 1123 label cap
    ],
)
def test_rejected_namespaces(namespaces):
    with pytest.raises(ValidationError):
        _create(namespaces=namespaces)


def test_a_service_account_may_be_longer_than_a_namespace():
    """A subdomain caps at 253, a label at 63 — they are genuinely different limits."""
    assert _create(service_accounts=["a" * 253]).service_accounts == ["a" * 253]
    with pytest.raises(ValidationError):
        _create(namespaces=["a" * 64])


def test_names_are_stripped():
    assert _create(namespaces=["  payments  "]).namespaces == ["payments"]


# --------------------------------------------------------------------------- #
# the wildcard — the one rejection with real blast radius
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field", ["service_accounts", "namespaces"])
@pytest.mark.parametrize("value", ["*", "pay*", "*-ci"])
def test_wildcards_are_rejected(field, value):
    """`["*"]` grants every workload in the cluster; that needs a human, not an API call."""
    with pytest.raises(ValidationError):
        _create(**{field: [value]})


@pytest.mark.parametrize("field", ["service_accounts", "namespaces"])
def test_the_wildcard_message_says_what_to_do_instead(field):
    with pytest.raises(ValidationError) as error:
        _create(**{field: ["*"]})

    message = str(error.value)
    assert "wildcard" in message
    assert "List each name explicitly" in message


# --------------------------------------------------------------------------- #
# access — the scope boundary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("capability", ["read", "write"])
def test_either_capability_alone_is_valid(capability):
    assert _create(access={capability: ["myapp"]}).access == {capability: ["myapp"]}


def test_both_capabilities_together_are_valid():
    access = {"read": ["myapp"], "write": ["billing"]}

    assert _create(access=access).access == access


@pytest.mark.parametrize(
    "access",
    [
        {},  # a role that reaches nothing
        {"read": []},
        {"read": ["  "]},
        {"read": ["myapp", ""]},
        {"read": ["dup", "dup"]},
        {"read/write": ["myapp"]},  # the combined key is not a capability
        {"admin": ["myapp"]},
        {"read": ["myapp"], "admin": ["billing"]},
        {"read": ["Not_A_Store"]},  # must be shaped like a KV store name
        {"read": ["a/b"]},
    ],
)
def test_rejected_access(access):
    with pytest.raises(ValidationError):
        _create(access=access)


def test_the_unknown_capability_message_lists_the_allowed_set():
    with pytest.raises(ValidationError) as error:
        _create(access={"admin": ["myapp"]})

    assert "allowed: read, write" in str(error.value)


def test_the_allowed_access_set_is_the_single_place_to_change():
    assert ALLOWED_KV_ACCESS_KEYS == frozenset({"read", "write"})


def test_access_is_a_separate_frozenset_from_the_store_roles():
    """Two pipeline contracts that agree today; either may drift without the other."""
    assert ALLOWED_KV_ACCESS_KEYS is not ALLOWED_ROLE_KEYS


def test_the_create_body_carries_no_policy_field():
    """No policy name, no HCL, no mount, no engine version — the pipeline derives those."""
    assert set(VaultKubernetesAuthCreate.model_fields) == {
        "file",
        "role_name",
        "role_description",
        "cluster",
        "service_accounts",
        "namespaces",
        "access",
        "ttl",
    }


# --------------------------------------------------------------------------- #
# ttl
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ttl", ["24h", "30m", "3600s", "7d"])
def test_valid_ttls(ttl):
    assert _create(ttl=ttl).ttl == ttl


@pytest.mark.parametrize("ttl", ["24", "h", "24hours", "-1h", "1.5h", ""])
def test_rejected_ttls(ttl):
    with pytest.raises(ValidationError):
        _create(ttl=ttl)


def test_ttl_is_optional():
    assert _create().ttl is None


# --------------------------------------------------------------------------- #
# the patterns themselves
# --------------------------------------------------------------------------- #
def test_the_k8s_patterns_are_not_the_fqdn_pattern():
    """`FQDN_PATTERN` accepts uppercase; Kubernetes does not, so reusing it would let a
    request pass here and fail in the cluster."""
    import re

    assert re.fullmatch(FQDN_PATTERN, "App01.Corp.Example.Com")
    assert not re.fullmatch(K8S_SERVICE_ACCOUNT_PATTERN, "App01.Corp")
    assert not re.fullmatch(K8S_NAMESPACE_PATTERN, "Payments")


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "field,value",
    [
        ("role_description", "new"),
        ("service_accounts", ["other"]),
        ("namespaces", ["other"]),
        ("access", {"write": ["myapp"]}),
        ("ttl", "1h"),
    ],
)
def test_update_accepts_any_single_field(field, value):
    assert getattr(VaultKubernetesAuthUpdate(**{field: value}), field) == value


def test_empty_update_is_rejected():
    """An edit that specifies nothing would open a pull request that changes nothing."""
    with pytest.raises(ValidationError):
        VaultKubernetesAuthUpdate()


def test_update_cannot_rename_or_move_cluster():
    """Both identify the role in Vault: changing either is a delete plus a create."""
    assert "role_name" not in VaultKubernetesAuthUpdate.model_fields
    assert "cluster" not in VaultKubernetesAuthUpdate.model_fields


def test_update_validates_the_same_way_as_a_create():
    with pytest.raises(ValidationError):
        VaultKubernetesAuthUpdate(namespaces=["*"])
    with pytest.raises(ValidationError):
        VaultKubernetesAuthUpdate(access={"admin": ["myapp"]})
    with pytest.raises(ValidationError):
        VaultKubernetesAuthUpdate(service_accounts=["Reader"])

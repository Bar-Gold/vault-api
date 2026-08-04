"""Validation of a Kubernetes service account binding.

A binding is three scalars, and all three are Kubernetes' vocabulary rather than ours — so
these mostly pin that the *cluster's* rules are enforced here rather than discovered at
admission time, long after the pull request merged.
"""
import pytest
from pydantic import ValidationError

from app.v1.vault.schemas import (
    K8S_NAMESPACE_MAX_LENGTH,
    K8S_SERVICE_ACCOUNT_MAX_LENGTH,
    K8sServiceAccountCreate,
)


def _create(**overrides):
    values = {
        "service_account": "vault",
        "namespace": "athena",
        "cluster": "dev",
    }
    values.update(overrides)
    return K8sServiceAccountCreate(**values)


def test_the_happy_path_keeps_all_three_parts():
    payload = _create()

    assert (payload.service_account, payload.namespace, payload.cluster) == (
        "vault",
        "athena",
        "dev",
    )


@pytest.mark.parametrize("field", ["service_account", "namespace", "cluster"])
def test_every_part_is_required(field):
    """All three identify the binding, so none can be defaulted or omitted."""
    values = {"service_account": "vault", "namespace": "athena", "cluster": "dev"}
    del values[field]

    with pytest.raises(ValidationError):
        K8sServiceAccountCreate(**values)


@pytest.mark.parametrize("field", ["service_account", "namespace", "cluster"])
def test_blank_parts_are_rejected(field):
    with pytest.raises(ValidationError):
        _create(**{field: "   "})


# --------------------------------------------------------------------------- #
# the wildcard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field", ["service_account", "namespace"])
def test_a_bare_wildcard_is_rejected(field):
    """`*` binds every workload in the cluster — that escalation needs a human."""
    with pytest.raises(ValidationError) as error:
        _create(**{field: "*"})

    assert "wildcard" in str(error.value)


@pytest.mark.parametrize("value", ["vault-*", "*-reader", "pay*ments"])
def test_an_embedded_wildcard_is_rejected(value):
    with pytest.raises(ValidationError):
        _create(service_account=value)


def test_the_wildcard_message_says_what_to_do_instead():
    with pytest.raises(ValidationError) as error:
        _create(namespace="*")

    message = str(error.value)
    assert "editing the values file directly" in message


# --------------------------------------------------------------------------- #
# RFC 1123 — a namespace is a label, a service account is a subdomain
# --------------------------------------------------------------------------- #
def test_a_service_account_may_carry_dots():
    """A ServiceAccount name is an RFC 1123 *subdomain*."""
    assert _create(service_account="system.vault.reader").service_account == (
        "system.vault.reader"
    )


def test_a_namespace_may_not_carry_dots():
    """A namespace is an RFC 1123 *label* — genuinely a different rule."""
    with pytest.raises(ValidationError):
        _create(namespace="pay.ments")


@pytest.mark.parametrize("field", ["service_account", "namespace"])
def test_uppercase_is_rejected(field):
    """The cluster rejects it at admission, so accepting it here defers the failure."""
    with pytest.raises(ValidationError):
        _create(**{field: "Vault"})


@pytest.mark.parametrize("value", ["-leading", "trailing-", "under_score", "sla/sh"])
def test_malformed_names_are_rejected(value):
    with pytest.raises(ValidationError):
        _create(namespace=value)


def test_a_namespace_at_the_label_limit_is_accepted():
    name = "a" * K8S_NAMESPACE_MAX_LENGTH

    assert _create(namespace=name).namespace == name


def test_a_namespace_over_the_label_limit_is_rejected():
    with pytest.raises(ValidationError):
        _create(namespace="a" * (K8S_NAMESPACE_MAX_LENGTH + 1))


def test_the_two_limits_are_different():
    """63 for a namespace, 253 for a service account — not a shared constant."""
    long_name = "a" * 100

    assert _create(service_account=long_name).service_account == long_name
    with pytest.raises(ValidationError):
        _create(namespace=long_name)


def test_a_service_account_over_the_subdomain_limit_is_rejected():
    with pytest.raises(ValidationError):
        _create(service_account="a" * (K8S_SERVICE_ACCOUNT_MAX_LENGTH + 1))


# --------------------------------------------------------------------------- #
# whitespace and the cluster
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field", ["service_account", "namespace"])
def test_surrounding_whitespace_is_stripped(field):
    assert getattr(_create(**{field: "  vault  "}), field) == "vault"


@pytest.mark.parametrize("value", ["dev", "prod", "prod-il-1"])
def test_valid_cluster_suffixes(value):
    assert _create(cluster=value).cluster == value


@pytest.mark.parametrize("value", ["Dev", "prod/il", "prod.il", "-dev"])
def test_malformed_cluster_suffixes_are_rejected(value):
    with pytest.raises(ValidationError):
        _create(cluster=value)


def test_the_binding_carries_no_capability():
    """Listing an account inside a store *is* the grant; the pipeline decides what it means."""
    payload = _create()

    for absent in ("access", "capability", "policies", "ttl", "role_name"):
        assert not hasattr(payload, absent)

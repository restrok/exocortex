"""Tests for reusable action-family canonicalization."""

from exocortex.actions import canonicalize_action_key
from exocortex.models import ActionSignature


def test_canonicalize_documentation_variants_to_one_family() -> None:
    """Different documentation descriptions share one intent family."""
    first = ActionSignature(
        action_key="update_documentation",
        subjects=["repository documentation"],
    )
    second = ActionSignature(
        action_key="standardize_repository_documentation",
        subjects=["repository contributors"],
    )

    assert canonicalize_action_key(first) == "repository.documentation.update"
    assert canonicalize_action_key(second) == "repository.documentation.update"


def test_canonicalize_access_keeps_route_outside_the_intent() -> None:
    """Terraform and Datahub access actions share intent but retain routes."""
    terraform = ActionSignature(
        action_key="grant_dataset_access_with_terraform",
        subjects=["service account"],
        objects=["BigQuery dataset"],
        tools=["Terraform"],
        route="terraform skill",
    )
    datahub = terraform.model_copy(
        update={
            "action_key": "grant_dataset_access_with_datahub",
            "tools": ["Datahub"],
            "route": "datahub MCP",
        }
    )

    assert canonicalize_action_key(terraform) == "iam.grant_access"
    assert canonicalize_action_key(datahub) == "iam.grant_access"


def test_canonicalize_unknown_intent_preserves_a_compact_key() -> None:
    """Unknown actions remain searchable instead of being discarded."""
    action = ActionSignature(action_key="custom.domain_operation")

    assert canonicalize_action_key(action) == "custom.domain_operation"

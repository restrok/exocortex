"""Tests for canonical labels and reversible aliases."""

from pathlib import Path

from exocortex.labels import LabelRegistry


def test_label_registry_unifies_common_cicd_variants(tmp_path: Path) -> None:
    """Equivalent labels resolve to one canonical key."""
    registry = LabelRegistry(tmp_path / "taxonomy.json")

    assert registry.canonicalize(["CI/CD", "cicd", "GitLab CI"]) == [
        "technology:gitlab-ci",
        "topic:cicd",
    ]


def test_label_registry_persists_an_alias_without_source_text(tmp_path: Path) -> None:
    """Alias metadata survives a new registry instance."""
    path = tmp_path / "taxonomy.json"
    registry = LabelRegistry(path)
    registry.register_alias("delivery pipelines", "topic:cicd")

    reloaded = LabelRegistry(path)

    assert reloaded.resolve("delivery pipelines") == "topic:cicd"


def test_acme_alias_means_acme_corp_organization(tmp_path: Path) -> None:
    """The local work vocabulary does not interpret acme as a region."""
    registry = LabelRegistry(tmp_path / "taxonomy.json")

    assert registry.resolve("acme") == "organization:acme-corp"
    assert registry.resolve("Acme Corp") == "organization:acme-corp"

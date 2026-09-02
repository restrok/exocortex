"""Canonical labels and reversible alias handling."""

from __future__ import annotations

import json
import re
from pathlib import Path

_CATEGORIES = {
    "auth",
    "environment",
    "organization",
    "project",
    "provider",
    "region",
    "repository",
    "role",
    "runtime",
    "system",
    "technology",
    "topic",
    "work_type",
}
_KNOWN_TECHNOLOGIES = {
    "cloud-run",
    "dataform",
    "docker",
    "gcp",
    "gitlab",
    "gitlab-ci",
    "grafana",
    "neo4j",
    "opentelemetry",
    "python",
    "terraform",
}
_ALIASES = {
    "ci/cd": "topic:cicd",
    "ci cd": "topic:cicd",
    "cicd": "topic:cicd",
    "continuous integration": "topic:cicd",
    "continuous delivery": "topic:cicd",
    "gitlab ci": "technology:gitlab-ci",
    "gitlab-ci": "technology:gitlab-ci",
    "pipelines": "topic:cicd",
    "acme": "organization:acme-corp",
    "acme corp": "organization:acme-corp",
    "acmecorp": "organization:acme-corp",
}


class LabelRegistry:
    """Persist canonical labels and aliases without retaining source text."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._aliases: dict[str, str] = dict(_ALIASES)
        self._load()

    def canonicalize(self, values: list[str]) -> list[str]:
        """Return deterministic, deduplicated canonical labels."""
        labels = {self.resolve(value) for value in values if value.strip()}
        return sorted(labels)

    def resolve(self, value: str) -> str:
        """Resolve one label while preserving its semantic category."""
        raw = _normalize_text(value)
        if not raw:
            return ""
        if raw in self._aliases:
            return self._aliases[raw]
        if ":" in raw:
            category, candidate = raw.split(":", 1)
            if category in _CATEGORIES and candidate:
                return f"{category}:{_slug(candidate)}"
        category = "technology" if raw in _KNOWN_TECHNOLOGIES else "topic"
        return f"{category}:{_slug(raw)}"

    def register_alias(self, alias: str, canonical: str) -> None:
        """Register a reversible alias and persist only taxonomy metadata."""
        normalized_alias = _normalize_text(alias)
        resolved = self.resolve(canonical)
        if normalized_alias and resolved:
            self._aliases[normalized_alias] = resolved
            self._save()

    def aliases(self) -> dict[str, str]:
        """Return a copy of the alias map for diagnostics and tests."""
        return dict(self._aliases)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        aliases = payload.get("aliases") if isinstance(payload, dict) else None
        if isinstance(aliases, dict):
            self._aliases.update(
                {str(key): str(value) for key, value in aliases.items()}
            )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps({"aliases": self._aliases}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(self._path)


def _normalize_text(value: str) -> str:
    """Normalize an input label without removing meaningful separators."""
    return re.sub(r"\s+", " ", value.strip().lower())


def _slug(value: str) -> str:
    """Create a stable label slug."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "unknown"

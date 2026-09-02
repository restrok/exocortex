"""Canonicalize action signatures into reusable intent families."""

from __future__ import annotations

import re

from exocortex.models import ActionSignature


def canonicalize_action_key(action: ActionSignature) -> str:
    """Return a stable, generic intent key for one action signature."""
    text = _action_text(action)
    tokens = set(re.findall(r"[a-z0-9]+", text.lower()))

    if _has_any(tokens, "grant", "permission", "access") and _has_any(
        tokens, "dataset", "service", "account", "role", "iam"
    ):
        return "iam.grant_access"
    if _has_any(tokens, "documentation", "docs", "doc") and _has_any(
        tokens, "update", "standardize", "rewrite", "refresh", "edit", "align"
    ):
        return "repository.documentation.update"
    if _has_any(tokens, "pytest", "test", "tests") and _has_any(
        tokens, "import", "imports", "conftest", "module", "path"
    ):
        return "repository.tests.repair_imports"
    if _has_any(tokens, "syntax", "compile", "compileall", "pytest", "test") and (
        _has_any(tokens, "run", "validate", "verify", "check")
    ):
        return "repository.changes.validate"
    if _has_any(tokens, "retention", "retain") and _has_any(
        tokens, "minimum", "enforce", "threshold", "count"
    ):
        return "retention.enforce_minimum"
    if _has_any(tokens, "pruner", "pruning") and _has_any(
        tokens, "dry", "preview", "simulate"
    ):
        return "pruner.safe_dry_run"
    if _has_any(tokens, "scheduler") and _has_any(
        tokens, "create", "configure", "trigger", "prepare", "parameterize", "add"
    ):
        return "cloud_scheduler.configure"
    if _has_any(tokens, "cloud", "run", "job") and _has_any(
        tokens, "execute", "run", "wait", "invoke"
    ):
        return "cloud_run_job.execute"
    if _has_any(tokens, "legacy", "unused") and _has_any(
        tokens, "remove", "delete", "cleanup"
    ):
        return "repository.maintenance.remove_legacy"
    if _has_any(tokens, "branch") and _has_any(
        tokens, "create", "checkout", "publish", "commit", "push"
    ):
        return "repository.branch.prepare_and_publish"
    if _has_any(tokens, "commit", "push", "publish") and _has_any(
        tokens, "documentation", "docs", "repository", "changes"
    ):
        return "repository.changes.publish"

    supplied = action.canonical_action_key or action.action_key
    return _compact_key(supplied)


def _action_text(action: ActionSignature) -> str:
    """Build the bounded text used for local intent classification."""
    return " ".join(
        [
            action.action_key,
            action.canonical_action_key,
            *action.subjects,
            *action.objects,
            *action.tools,
            action.route,
            action.outcome,
        ]
    )


def _has_any(tokens: set[str], *values: str) -> bool:
    """Return whether a token set contains one of the supplied values."""
    return bool(tokens.intersection(values))


def _compact_key(value: str) -> str:
    """Normalize an LLM key while retaining unknown intent information."""
    normalized = re.sub(r"[^a-z0-9._-]+", "_", value.lower()).strip("_-")
    parts = [part for part in normalized.split(".") if part]
    if 2 <= len(parts) <= 4:
        return ".".join(parts)
    return normalized[:120]

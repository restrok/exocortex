"""Deterministic redaction for potentially sensitive source material."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from exocortex.models import SanitizationFinding, SanitizedContent


class Sanitizer:
    """Remove common secret formats before content leaves the local process."""

    _RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
        (
            "private_key",
            re.compile(
                r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?"
                r"-----END (?:[A-Z ]+ )?PRIVATE KEY-----",
                re.DOTALL,
            ),
        ),
        (
            "authorization_header",
            re.compile(r"(?im)^(authorization:\s*)(?:bearer|basic)\s+\S+$"),
        ),
        (
            "bearer_token",
            re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{16,}\b"),
        ),
        ("openai_key", re.compile(r"\bsk-[a-zA-Z0-9_-]{20,}\b")),
        ("github_token", re.compile(r"\bgh[pousr]_[a-zA-Z0-9]{20,}\b")),
        ("aws_key_id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
        (
            "password_assignment",
            re.compile(
                r"(?im)\b(password|passwd|secret|api[_-]?key|token)"
                r"\s*[:=]\s*([^\s\"']{8,})"
            ),
        ),
    )
    _HIGH_ENTROPY = re.compile(r"\b[a-zA-Z0-9+/=_-]{40,}\b")
    _PROMPT_INJECTION_RULES = (
        re.compile(r"(?i)\bignore\s+(?:all\s+)?previous\s+instructions\b"),
        re.compile(r"(?i)\b(?:system|developer)\s*:\s*"),
        re.compile(r"(?i)\bdo\s+not\s+follow\s+the\s+instructions\b"),
        re.compile(r"(?i)\bdo\s+not\s+(?:cite|mention|use)\b"),
        re.compile(r"(?i)<\|(?:system|assistant|user|im_start|im_end)\|>"),
        re.compile(r"(?i)<\s*untrusted[_ -]source\b[^>]*>"),
        re.compile(r"(?i)\bignora\s+(?:todas\s+)?las\s+instrucciones\b"),
    )

    def sanitize(self, text: str) -> SanitizedContent:
        """Redact known secret formats and high-entropy opaque values."""
        source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        findings: list[SanitizationFinding] = []
        sanitized = text

        for kind, pattern in self._RULES:
            sanitized, count = pattern.subn(f"[REDACTED:{kind}]", sanitized)
            if count:
                findings.append(SanitizationFinding(kind=kind, count=count))

        sanitized, count = self._redact_high_entropy(sanitized)
        if count:
            findings.append(SanitizationFinding(kind="high_entropy", count=count))

        return SanitizedContent(
            text=sanitized,
            source_hash=source_hash,
            findings=findings,
        )

    def audit(self, text: str) -> list[SanitizationFinding]:
        """Return only high-confidence secret formats suitable for audit reports."""
        findings: list[SanitizationFinding] = []
        for kind, pattern in self._RULES:
            count = sum(1 for _ in pattern.finditer(text))
            if count:
                findings.append(SanitizationFinding(kind=kind, count=count))
        return findings

    def sanitize_metadata(self, text: str) -> SanitizedContent:
        """Sanitize metadata without redacting stable opaque identifiers."""
        source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        findings: list[SanitizationFinding] = []
        sanitized = text
        for kind, pattern in self._RULES:
            sanitized, count = pattern.subn(f"[REDACTED:{kind}]", sanitized)
            if count:
                findings.append(SanitizationFinding(kind=kind, count=count))
        return SanitizedContent(
            text=sanitized,
            source_hash=source_hash,
            findings=findings,
        )

    def contains_prompt_injection(self, text: str) -> bool:
        """Return whether text contains a high-confidence injection marker."""
        return any(pattern.search(text) for pattern in self._PROMPT_INJECTION_RULES)

    def _redact_high_entropy(self, text: str) -> tuple[str, int]:
        """Redact opaque strings while retaining obvious harmless identifiers."""
        replacements = 0

        def replace(match: re.Match[str]) -> str:
            nonlocal replacements
            value = match.group(0)
            if self._looks_like_high_entropy_secret(value):
                replacements += 1
                return "[REDACTED:high_entropy]"
            return value

        return self._HIGH_ENTROPY.sub(replace, text), replacements

    @staticmethod
    def _looks_like_high_entropy_secret(value: str) -> bool:
        """Return whether a long token has enough character diversity to redact."""
        if re.fullmatch(r"[a-fA-F0-9]{40,}", value):
            return False
        diversity = len(set(value))
        has_digit = any(character.isdigit() for character in value)
        has_letter = any(character.isalpha() for character in value)
        return diversity >= 12 and has_digit and has_letter


def findings_summary(findings: Iterable[SanitizationFinding]) -> str:
    """Return a compact, non-sensitive summary of sanitizer findings."""
    return ", ".join(f"{finding.kind}:{finding.count}" for finding in findings)

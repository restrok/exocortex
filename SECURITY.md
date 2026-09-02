# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.2.x   | :white_check_mark: |
| < 0.2   | :x:                |

## Security Architecture & Data Privacy

Exocortex is designed with a **privacy-first, zero-leakage architecture** for engineering teams:

1. **Deterministic Pre-Sanitization**: All session inputs, terminal rollouts, and raw texts pass through deterministic regex and high-entropy secret scrubbers (`Sanitizer`) *before* reaching any LLM gateway or vector index.
2. **Local-First Source of Truth**: The canonical knowledge store is local Markdown (`Vault`), stored on your local disk or enterprise infrastructure. Neo4j acts purely as a rebuildable query projection.
3. **No Raw Rollouts Retained**: Raw terminal and session transcripts are never stored permanently; only sanitized and validated structured knowledge notes are retained.
4. **Air-Gapped Compatibility**: Exocortex can be run 100% locally with Ollama / local embeddings without transmitting any data over the public internet.

## Reporting a Vulnerability

If you discover a security vulnerability or potential credential leakage issue within Exocortex:

1. **Do not create a public GitHub issue.**
2. Please report vulnerabilities privately via email to: `siriofederico@gmail.com`.
3. Provide a clear description of the vulnerability, steps to reproduce, and potential impact.
4. You will receive an initial response within 48 hours acknowledging receipt of your report.

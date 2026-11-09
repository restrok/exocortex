# AGENTS.md - Exocortex Engineering Standards & Autonomous Agent Guardrails

This document defines the mandatory architectural, security, performance, and SDLC guardrails that all human engineers and autonomous AI coding agents MUST follow when working on the **Exocortex** repository.

---

## 1. Branch-Only Workflow, Semantic Commits & Conventional PRs (Mandatory)

1. **Zero Direct Commits to `main` or `master`**:
   - Working, committing, or pushing directly on `main` or `master` is **strictly forbidden** (and blocked by pre-commit hooks).
   - Every task, bugfix, security patch, or refactoring MUST begin by creating a dedicated branch from an up-to-date `main`:
     ```bash
     git checkout main && git pull --ff-only
     git checkout -b <type>/<scope>-<short-kebab-description>
     ```
   - Allowed branch prefixes (`<type>`): `feat/`, `fix/`, `security/`, `perf/`, `refactor/`, `test/`, `docs/`, `ci/`, `build/`, `chore/`, `hotfix/`.

2. **Semantic Commits (Conventional Commits v1.0.0)**:
   - Every commit MUST be atomic and formatted strictly as:
     ```text
     <type>(<scope>): <imperative summary in lowercase, max 72 chars>

     [optional body explaining WHY the change was made and root cause]

     [optional footer: BREAKING CHANGE: ... or Refs/Closes #issue]
     ```
   - **Allowed `<type>` values**:
     - `feat`: New feature, MCP tool, CLI command, or capability.
     - `fix`: Bug fix, race condition fix, or resource leak remediation.
     - `security`: Vulnerability remediation, auth hardening, sanitization, or container security.
     - `perf`: Latency reduction, caching, query optimization, or memory improvement.
     - `refactor`: Code modularization or restructuring without changing external behavior.
     - `test`: Adding or updating unit, integration, or evaluation tests.
     - `docs`: Documentation, runbooks, architecture records, or `AGENTS.md` updates.
     - `ci`: GitHub Actions workflows, pipelines, or quality gate automation.
     - `build`: `Dockerfile`, `pyproject.toml`, `uv.lock`, or packaging changes.
     - `chore`: Maintenance, tooling configuration, or cleanup.
   - **Allowed `<scope>` values**: `mcp`, `service`, `graph`, `vault`, `assets`, `dashboard`, `ingest`, `sanitize`, `gateway`, `maintenance`, `spaces`, `checkpoint`, `cli`, `scheduler`, `telemetry`, `evals`, `config`, `docker`, `ci`, `deps`, `rules`.

3. **Conventional Pull Requests**:
   - All changes MUST be merged into `main` via a Pull Request.
   - **PR Title** MUST follow the exact Semantic Commit format: `<type>(<scope>): <summary>`.
   - **PR Description** MUST complete `.github/pull_request_template.md`, including:
     - Context & Root Cause.
     - Security & Resource Lifecycle Checklist.
     - Verification & Quality Gate Results (`ruff`, `mypy`, `bandit`, `pytest`).
     - Before vs. After Performance/Quality Delta when applicable.

---

## 2. Zero-Tolerance Security Guardrails

1. **Parameterized Cypher Queries Only (`src/exocortex/graph.py`)**:
   - NEVER build Cypher queries using f-strings, `%` formatting, or string concatenation with untrusted values.
   - ALWAYS pass values via Neo4j driver parameters (`session.run(statement, **params)`). Node labels may only be interpolated from the static `_LABELS` allowlist.

2. **Lucene Full-Text Query Escaping (`src/exocortex/graph.py`)**:
   - Every search query passed to `CALL db.index.fulltext.queryNodes(...)` MUST be sanitized through `_escape_lucene_query()` to neutralize Lucene special operators (`+ - && || ! ( ) { } [ ] ^ " ~ * ? : \ /`).

3. **Deterministic Resource Lifecycle (`Neo4jStore` & `sqlite3`)**:
   - Never rely on garbage collection or `__del__` destructors to close Neo4j drivers or SQLite connections.
   - Always use context managers (`with self._graph_store() as store:` or `try ... finally: store.close()`) for Neo4j and `with self._get_db_connection() as conn:` (which guarantees `conn.close()` in `finally:`) for SQLite in `AssetStore`.

4. **Path Traversal Validation Before Filesystem I/O (`assets.py`, `vault.py`)**:
   - Always resolve target paths and verify `target_path.resolve().is_relative_to(base_root.resolve())` **before** executing `mkdir()`, `copy2()`, `move()`, or file writes.

5. **Dashboard & MCP Endpoint Security (`dashboard.py`, `mcp_server.py`)**:
   - Escape `</` (`<\/`) and `<script` (`<\u0073cript`) before embedding JSON payloads inside HTML `<script>` blocks to prevent Stored XSS.
   - Filter out private spaces (`personal-*`) from unauthenticated dashboard summaries (`recent_notes`) by default.
   - Include HTTP security headers (`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`) and validate `BRAIN_MCP_AUTH_TOKEN` via constant-time `hmac.compare_digest` when configured.

6. **Zero Plaintext Secrets & Non-Root Containers (`Dockerfile`, `sanitize.py`)**:
   - Never hardcode secrets, tokens, or credentials in code or commit history.
   - Containers MUST run as the unprivileged user `exocortex` (`UID/GID 1001`, matching host bind-mount ownership).

---

## 3. Architecture, Performance & Modularity Standards

1. **Canonical Module Responsibilities (`src/exocortex/`)**:
   - `config.py`: `Pydantic` `BaseSettings` environment configuration and validation.
   - `models.py`: Strict `Pydantic` v2 domain models and response envelopes.
   - `graph.py`: `Neo4jStore` driver lifecycle, schema constraints, Lucene escaping, and parameterized Cypher/vector queries.
   - `vault.py`: Canonical Markdown persistence with `(st_mtime_ns, st_size)` in-memory cache and `O(1)` `_id_to_path` index.
   - `assets.py`: Content-addressed asset storage, append-only JSONL manifest, and SQLite indexing.
   - `checkpoint.py`: Session rollout signatures, checkpoint state validation, and note fingerprinting.
   - `service.py`: High-level orchestration (hybrid search, memory creation, workflow recommendation, reflection, intents).
   - `mcp_server.py`: FastMCP streamable-HTTP server, tool bindings, auth checks, and dashboard routes.
   - `dashboard.py`: Real-time metrics aggregation (with TTL health caching) and XSS-safe HTML rendering.
   - `ingest.py` & `sanitize.py`: Secret redaction, prompt-injection detection, and batch claim extraction.
   - `maintenance.py` & `scheduler.py`: Deduplication, graph hygiene, and scheduled background jobs.
   - `gateway.py`: Resilient HTTP client for LLM extraction and vector embeddings with retries and deadlines.

2. **Modularity & File Size Limit**:
   - New modules MUST NOT exceed **600 lines of code**. When modifying large legacy modules (such as `service.py`), extract cohesive domain logic into dedicated submodules while preserving backward-compatible re-exports.

3. **Strict Static Typing**:
   - All functions and methods MUST include complete Python 3.12+ type annotations.
   - Use `dict[str, Any]` (not `dict[str, object]`) for dynamic JSON structures, and `Literal[...]` type aliases from `models.py` for constrained state fields.

---

## 4. Mandatory Verification & Quality Gates (Pre-PR Checklist)

No commit or Pull Request may be submitted unless all 5 verification gates pass with **zero errors and zero warnings**:

```bash
# Run all gates via Make:
make check

# Or execute individually via uv:
uv run --extra dev ruff check src tests
uv run --extra dev ruff format --check src tests
uv run --extra dev mypy src
uv run --extra dev bandit -r src -c pyproject.toml
uv run --extra dev pytest
```

- **Zero-Warning Policy**: `pytest` enforces `filterwarnings = ["error::DeprecationWarning", "error::ResourceWarning"]`. Any leaked Neo4j driver or unclosed SQLite connection fails the test suite immediately.

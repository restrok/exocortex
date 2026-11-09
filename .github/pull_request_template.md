## Summary
<!-- Concise description of WHAT changed and WHY. Title must follow: <type>(<scope>): <summary> -->

## Type of Change
- [ ] `feat`: New capability or MCP tool
- [ ] `fix`: Bug fix or resource lifecycle remediation
- [ ] `security`: Security hardening or vulnerability fix
- [ ] `perf`: Performance / caching / latency optimization
- [ ] `refactor`: Modularization without functional change
- [ ] `test` / `ci` / `docs` / `build` / `chore`: Tooling, tests, or guardrails

## Security & Resource Lifecycle Checklist
- [ ] All Neo4j queries use parameterized Cypher (`$param`) and `_escape_lucene_query()` for full-text search
- [ ] All `Neo4jStore` and SQLite connections are deterministically closed (`with` / `finally:`)
- [ ] Filesystem writes validate `Path.resolve().is_relative_to(root.resolve())` prior to I/O
- [ ] Zero hardcoded secrets, tokens, or private space leakage

## Verification & Quality Gates
- [ ] `uv run --extra dev ruff check src tests` (0 errors)
- [ ] `uv run --extra dev ruff format --check src tests` (formatted)
- [ ] `uv run --extra dev mypy src` (0 errors)
- [ ] `uv run --extra dev bandit -r src -c pyproject.toml` (0 issues)
- [ ] `uv run --extra dev pytest` (100% passing, 0 `DeprecationWarning` / `ResourceWarning`)

## Performance & Quality Delta (Before vs. After)
<!-- Optional: Include benchmark latency, test duration, or coverage comparison -->

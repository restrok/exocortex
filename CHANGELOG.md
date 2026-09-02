# Changelog

All notable changes to Codex Brain are documented here.

## [Unreleased]

## [v0.1.9] - 2026-08-11

### Added

- Added a total wall-clock deadline for gateway requests, including retries,
  plus a dedicated shorter extraction-canary timeout.
- Added per-attempt `exocortex.gateway.request` spans with bounded timeout,
  elapsed-time, status, retryability, and deadline metadata.

### Fixed

- Hanging gateway transports can no longer keep extraction or canary requests
  running for hours; ingestion budgets also account for suspended wall-clock
  time.

## [v0.1.8] - 2026-08-10

### Added

- Bulk Vault note lookup and bounded ingestion/search spans with candidate,
  batch, checkpoint, and gateway cardinality attributes.
- Configurable bounded retries for transient gateway 502/503/504 responses.

### Fixed

- Avoided repeated full-Vault scans during ingestion and hybrid search.
- Propagated ingestion time budgets to extraction requests and reduced
  checkpoint writes from once per record to once per batch.
- OTEL now reports the actual package version instead of a stale hardcoded
  version.

## [v0.1.7] - 2026-08-09

### Fixed

- Literal entity/object fallback retrieval recovers related Vault context when
  the graph projection misses a technical note.
- Spanish elimination verb forms such as `eliminamos` are classified as action
  intent, preserving abstention when only dataset context is available.

## [v0.1.6] - 2026-08-09

### Fixed

- Technical query labels now gate generic pattern promotion, preventing a
  Terraform query from promoting an unrelated pattern from another technology.
- Scoped abstentions now always expose an explicit reason such as
  `no_evidence_for_scope`.
- Timeline responses report their source-date basis and coverage diagnostics for
  notes with missing or divergent source dates.

## [v0.1.5] - 2026-08-09

### Fixed

- Fixed Neo4j full-text searches using a `query` parameter that collided with
  the driver method signature and incorrectly forced embedding degradation.
- Generic pattern promotion now requires sufficient lexical intent overlap,
  leaving adjacent patterns in related context.
- Timeline and note retrieval expose explicit no-result and claim-extraction
  metadata for clearer provenance.

## [v0.1.4] - 2026-08-09

### Changed

- Generic searches now prioritize verified, provider-agnostic pattern knowledge.
- Scoped adapters and examples remain available as related context instead of
  being promoted to universal answers.
- Generic searches abstain with an explicit pattern-gap reason when only
  specific implementations are available.

## [v0.1.3] - 2026-08-09

### Added

- Optional operational context for ranking workflow recommendations by role,
  domains, common tasks, preferred tools, and low-priority work.
- Separate evidence, quality, relevance, actionability, specificity, and
  genericness scores in workflow search results.
- A `deprioritize` recommendation level for workflows with weak contextual
  fit, without relaxing the evidence gate.
- Action signatures for normalized operations, resources, tools, routes, and
  outcomes, allowing one action to expose one or multiple implementation paths.
- Conservative search output with canonical verification, answer and
  exploration modes, explainable match reasons, and `claim_support` states.
- Lexical-only degradation behavior that abstains instead of exposing weak
  semantic candidates.
- `brain_record_search_feedback` for sanitized relevance feedback on search
  results.
- Structured knowledge levels for reusable patterns, scoped adapters, examples,
  and decisions.
- Explicit scope metadata for organization, provider, runtime, region,
  authentication, environment, project, repository, and role.
- Deterministic pattern consolidation from independent implementations sharing
  a `pattern_key`, preserving provenance without claiming execution.
- Organization organization alias handling for the local `organization` vocabulary.

## [v0.1.2] - 2026-08-07

### Added

- Evidence-gated workflow reflection that can promote repeated,
  positively-supported `assistant_suggestion` claims without explicit user
  confirmation.
- Incremental reflection state and updateable workflow recommendations with
  confidence, usage, feedback, and quarantine metadata.
- Multi-signal historical context retrieval for reflection using canonical
  labels, lexical terms, claim keys, and optional Neo4j vector matches.
- Workflow feedback handling and richer workflow metadata in search and MCP
  responses.

### Changed

- Reflection now distinguishes independent rollout sessions from thematic
  segments when validating repeated evidence.
- Existing workflows are matched conservatively and updated instead of being
  duplicated; ambiguous matches are quarantined.
- Workflow matching now keeps materially different action routes separate while
  still updating the same route when it is rediscovered.
- Documentation, maintenance, graph projection, and Codex session handling
  were aligned with the schema-v2 memory model.

### Verification

- Full test suite passes with Ruff checks clean.
- Docker image builds successfully and the Compose stack remains valid.
- Full-vault reflection processed 331 existing notes without ingesting new
  sessions and produced evidence-backed workflows and aliases.

# Codex Brain: Experiences, Labels, and Nightly Consolidation

## Goal

Build a local work knowledge system that learns from Codex sessions without
sharing data with the separate personal deployment. The Brain retains only
sanitized Markdown, while Neo4j remains a rebuildable projection.

## Learning Cycle

1. The scheduler reads closed sessions from the read-only
   `~/.codex/sessions` mount.
2. Each rollout is split into stable thematic experiences. A session path and
   event range identify an experience independently of its content hash.
3. The sanitizer removes secrets before persistence or gateway calls.
4. The gateway extracts labels, claims, evidence spans, and evidence status.
   Fallback extraction is `unknown`, searchable with a penalty, and cannot
   activate a workflow.
5. Labels are canonicalized by category, with reversible aliases. Generated
   and human labels remain separate.
6. The reflection model creates a workflow only when independent evidence
   includes a confirmed success or decision. Assistant suggestions may become
   eligible after supported positive evidence repeats across at least two
   independent sessions. Contradictions block activation.
   Reflection context uses an OR union of canonical labels, lexical terms,
   claim keys, and optional Neo4j vector matches before deterministic evidence
   validation.
7. Canonical notes, claims, workflows, labels, and source relationships are
   projected into Neo4j.

The cycle runs once per local calendar day at 03:00 in
`America/Argentina/Buenos_Aires`, recovers a missed run after startup, and uses
a persistent lock and status record.

## Interfaces

- `brain_search`, `brain_get`, and `brain_list_by_date` provide grounded memory
  retrieval.
- `brain_list_by_label` connects experiences by canonical labels.
- `brain_record_search_feedback` stores sanitized relevance feedback for
  retrieval evaluation.
- `brain_recommend_workflow` and `brain_get_workflow` expose active procedures
  to Codex dynamically through MCP.
- `brain_learning_status` and `brainctl learning-status` expose non-sensitive
  progress.
- `brainctl reflect` runs one reflection cycle manually.

The `codex-work-brain` skill recommends a workflow before repeated work and
then retrieves its evidence. User instructions, `AGENTS.md`, repository rules,
and normal command approvals always take precedence.

## Asymmetric Retrieval Trust

Ingestion and processing maximize recall: sanitized notes remain in the Vault
even when they are proposed, incomplete, fallback, or contradictory. Their
source references, dates, project/repository labels, confidence, claims,
actions, and evidence status are retained for later evaluation.

Search maximizes internal recall but keeps output conservative. The default
`answer_mode=conservative` verifies canonical notes and separates direct
evidence from related context. `answer_mode=exploration` exposes broader
candidates without allowing them to support factual conclusions. Search
results classify claim support as `direct`, `context_only`, `contradictory`, or
`unknown`; a context match never proves that an action occurred.

When an action has no direct evidence, the response abstains from confirming
it and reports that related context was found without evidence of execution.
Embedding degradation removes semantic-only candidates from the verified
output. Explicit or high-confidence project/repository scope can filter
results; weak inferred scope only affects ranking. Search relevance feedback
is persisted separately through `brain_record_search_feedback`.

## Operational Context and Recommendation Quality

Workflow eligibility remains evidence-gated and deterministic. Recommendation
quality is evaluated separately across evidence confidence, procedural quality,
actionability, specificity, and relevance. An optional operational context can
contain a role, domains, common tasks, preferred tools, and low-priority work.
It only changes ranking and interaction guidance; it cannot activate weak
evidence or invalidate a supported workflow. Workflows with low contextual fit
are returned as `deprioritize`, while strong evidence and sufficient quality
may be returned as `auto_apply`.

The context is configured with `BRAIN_OPERATIONAL_*` variables in
`.env.example`. Comma-separated values keep the configuration portable across
local and Docker deployments. Leaving it disabled preserves the existing
confidence-based recommendation behavior.

## Action-Centered Workflow Discovery

Experiences also carry normalized action signatures: an action key, subjects,
objects, tools, route, and observed outcome. Reflection groups evidence by the
action before generating workflows. An action may have one known path or many;
materially different tools or routes remain separate workflows under the same
action. Deduplication therefore updates the same action path without collapsing
valid alternatives.

## Migration and Persistence

Existing whole-session notes are retained for audit but marked `superseded`
after segmented replacements exist. Superseded notes are excluded from normal
search, reflection, and workflow recommendations. Markdown remains the
canonical source; Neo4j can be rebuilt at any time.

## Security

- The MCP endpoint and Neo4j ports bind to loopback.
- Only the scheduler sees the local Codex sessions mount, and only read-only.
- Raw sessions, credentials, and secret-like values are excluded from Vault,
  graph properties, logs, gateway payloads, and exports.
- Work and personal deployments use separate repositories, data, credentials,
  gateways, MCP registrations, and backups.

## Acceptance

- Closed sessions ingest idempotently and split into meaningful experiences.
- Labels unify common variants such as `CI/CD`, `cicd`, and `GitLab CI` without
  collapsing merely related concepts.
- Unknown, proposed, failed, or fallback evidence cannot create an active
  recommended workflow.
- Repeated evidence-backed `assistant_suggestion` claims may create a workflow
  candidate without requiring explicit user confirmation.
- Confirmed evidence from one or more sessions creates one cited, updateable
  workflow candidate with a continuous confidence score.
- A failed gateway cycle retries without advancing reflection state.
- Ingestion retains proposed, incomplete, fallback, and contradictory notes
  with their provenance instead of turning them into confirmed facts.
- Search distinguishes direct evidence from context-only and weak candidates;
  conservative mode abstains when an action is not directly supported.
- Degraded embedding search uses lexical evidence only and never presents
  semantic-only candidates as verified facts.
- `ruff`, `pytest`, Compose validation, skill validation, and `brainctl audit`
  pass.

## Quality and Trust v2

Schema-v2 response envelopes expose `status`, `method`, `data`, and bounded
diagnostics. Extraction uses claims with source evidence, and sessions are
processed in bounded overlapping windows. Hybrid retrieval combines lexical
and vector candidates with RRF, quality penalties, graph signals, and an
initial abstention threshold of `0.45`. Workflow recommendations start at
confidence `0.50`; scores below `0.80` require confirmation, scores below
`0.30` are quarantined, and steps may use direct source or exact evidence when
there is no unresolved contradiction. Feedback adjusts workflow confidence and
is projected to Neo4j after the Vault write.

The frozen `evals/golden-v1.jsonl` set contains 60 sanitized cases across
retrieval, workflows, temporal queries, ambiguity, out-of-domain requests, and
adversarial content. `brainctl eval`, `make quality`, and `make quality-live`
report Recall@5, MRR@10, Precision@3, nDCG@5, abstention accuracy, and claim
support.

`brainctl repair apply` creates a sanitized backup before deterministic path,
schema, state, and duplicate repairs. `brainctl backfill --all` re-extracts
sources in resumable batches and is separate from the daily scheduler. Daily
Codex ingestion uses per-segment hashes, deterministic trivial-content
filtering, bounded extraction batches, and hard call/time limits.

## Future Improvements

- Batch embeddings during sync after validating the bounded extraction path.
- Add a dedicated maintenance status view for manually scheduled backfills.
- Keep progress logs free of prompts, note contents, sensitive titles,
  credentials, and headers.

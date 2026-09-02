# RFC 001: Centralized Multi-User Architecture & Label-First Knowledge Graph

- **Author:** Federico Alejandro Sirio & Antigravity Engineering
- **Status:** Proposed / Accepted Roadmap
- **Date:** 2026-09-02
- **Related PRs:** [#8](https://github.com/restrok/exocortex/pull/8)

---

## 1. Context & Motivation

Originally, Exocortex ran locally alongside agent clients (like OpenAI Codex or local CLI tools), directly reading session files from the local filesystem (`~/.codex/sessions`) and relying on rigid workspace separation ("spaces" such as `work` vs `personal`).

While this workstation-monolithic approach worked for a single engineer testing locally, **it does not scale**:
1. **Coupled Client & Brain:** Binds the memory system to a single developer's physical machine and filesystem.
2. **Impractical Network Mounts:** Mounting workstation folders over the network to a central server is fragile, insecure, and an anti-pattern.
3. **Rigid Space Silos ("Personal" vs "Work"):** In practice, hardcoded space boundaries cause fragmentation. Engineers work across cross-cutting boundaries; separating knowledge into rigid directory silos leads to duplication, lost context, or users simply dumping everything into one bucket.

This RFC defines the evolution of Exocortex into a **centralized, multi-user, label-driven engineering memory service** accessed exclusively via standard **Model Context Protocol (MCP)** over HTTP/SSE.

---

## 2. Core Architecture Principles

### 2.1 Decoupled, Network-First MCP Transport
Exocortex runs as an independent headless service on dedicated infrastructure (e.g. Docker macvlan on Lenovo server at `192.168.89.30`).
- **No Local Filesystem Binding:** Neither the client nor the server shares filesystems or bind-mounts.
- **Pure MCP Transport:** All ingestion (`brain_remember`, `brain_ingest_session`), search (`brain_search`), and workflow retrieval (`brain_recommend_workflow`) occur over streamable HTTP / Server-Sent Events (SSE).
- **Multiple Concurrent Clients:** Antigravity on Windows, Codex, Cursor, CI/CD pipelines, or other teammates' workstations can concurrently interact with the central brain.

### 2.2 Unified Knowledge Graph with Label-First Taxonomy (Replacing Rigid Spaces)
Instead of partitioning notes into rigid database spaces, Exocortex maintains a **single unified knowledge graph**. 

Differentiation, discovery, and filtering are governed by **rich, multi-dimensional taxonomy labels**:

```
                              ┌───────────────────────────┐
                              │  Unified Knowledge Graph  │
                              │       (Vault + Neo4j)     │
                              └─────────────┬─────────────┘
                                            │
               ┌────────────────────────────┼────────────────────────────┐
               ▼                            ▼                            ▼
      Technology & Domain               User & Author                 Context & Scope
      -------------------               -------------                 ---------------
      • technology:docker               • author:fsirio               • domain:infrastructure
      • technology:terraform            • author:juan_dev             • domain:data-platform
      • topic:asymmetric-routing        • role:devops                 • sensitivity:internal
      • topic:cloud-run                 • role:dataeng                • type:incident
```

**Why Labels Outperform Rigid Spaces:**
- **Fluid Boundaries:** A pattern touching both Docker and BigQuery isn't trapped in a silo; it's tagged with both `topic:infrastructure` and `topic:data-pipeline`.
- **Granular Queries:** Agents can query by intersection (e.g. `technology:terraform AND topic:ci-cd`) rather than browsing separate spaces.
- **Zero Friction:** Users don't need to decide "is this note personal, work, or infrastructure?" — the extraction engine automatically assigns taxonomy labels.

---

## 3. Multi-User Model & Identity Attribution

Every session ingested or memory recorded carries user attribution:

### 3.1 Data Model Extensions
```python
class SourceReference(BaseModel):
    id: str
    locator: str
    user_id: str | None = None          # e.g., "fsirio", "jdoe"
    author_role: str | None = None      # e.g., "devops", "data_engineer"
    occurred_on: date | None = None
```

In the markdown frontmatter of each generated Vault note:
```yaml
---
id: 7fece3ed-cae5-4e14-be4c-88e1cd991530
title: Docker Macvlan Asymmetric Routing Resolution
author_id: fsirio
author_role: devops
labels:
  - technology:docker
  - topic:macvlan
  - topic:networking
  - domain:infrastructure
---
```

In Neo4j, user nodes are projected into the graph:
```cypher
(:User {id: "fsirio", name: "Federico Sirio"})
  -[:AUTHORED]-> (:Note)
  -[:OBSERVED]-> (:Claim)
```

---

## 4. Automated User Role Profiling & Competency Graph

Rather than requiring manual role assignment in an admin console, the central **Scheduler** periodically analyzes each user's ingested interactions and claims.

### 4.1 Periodic Profiling Job
A nightly or periodic task runs across newly ingested sessions:
1. **Activity & Tool Usage Analysis:**
   - Evaluates commands, tools, and libraries invoked (e.g., `terraform`, `gcloud`, `kubectl`, `docker` vs. `spark`, `dbt`, `bigquery`, `pandas`).
2. **Domain Classification:**
   - Computes domain affinity scores across engineering specializations:
     - `devops` / `sre`: High volume of infrastructure, networking, container, and orchestration patterns.
     - `data_engineer`: High volume of data pipelines, ETL, warehouse queries, and distributed compute patterns.
     - `backend` / `software_engineer`: High volume of application frameworks, APIs, unit tests, and business logic.
3. **Graph Role Attachment:**
   - The scheduler assigns or updates competency edges in Neo4j:
     ```cypher
     (:User {id: "fsirio"})-[:COMPETENCY {score: 0.96, domain: "infrastructure"}]->(:Role {name: "devops"})
     ```

### 4.2 Role-Weighted Retrieval (Credibility Ranking)
When an agent searches for solutions:
- A query matching `topic:networking` will weight results authored by users with high `devops`/`sre` competency higher in the Reciprocal Rank Fusion (RRF) algorithm.
- Solves team trust: answers grounded in verified patterns from the team's domain specialists naturally surface first.

---

## 5. Autonomous Memory Retention (Hindsight Style)

The system enforces **proactive, autonomous retention**:
- Agents do not wait for the human user to say *"remember this"* or *"save this note"*.
- When an agent completes a non-trivial diagnosis, verifies a fix, or defines an infrastructure runbook, it invokes `brain_remember` or `brain_ingest_session` autonomously over MCP.
- Client lifecycle events (e.g., Antigravity `Stop` hooks) can automatically forward transcript diffs to the MCP server.

---

## 6. Implementation Roadmap

| Phase | Milestone | Status |
| :--- | :--- | :--- |
| **Phase 1** | Decoupled server on macvlan, local LM Studio Gemma 4 support, zero-mount MCP ingestion (`brain_ingest_session`, `brain_remember`). | **Completed (PR #8)** |
| **Phase 2** | Add `user_id` / `author_id` parameter to MCP ingestion tools; transition search filters from rigid spaces to dynamic label filtering. | **Next** |
| **Phase 3** | Implement the background User Role Profiler in `scheduler.py` to analyze tool/domain frequency per `user_id`. | **Planned** |
| **Phase 4** | Incorporate author role credibility weighting into hybrid RRF search in Neo4j. | **Planned** |

---
name: codex-work-brain
description: Search the local work Codex Brain MCP server for prior projects, decisions, debugging history, patterns, incidents, or "how we did X". Use when an answer may benefit from durable work memory.
---

# Codex Work Brain

Use the configured local Codex Brain MCP server as a grounded work-memory
source. It stores sanitized knowledge in the local work Vault.

1. For repeated work, call `brain_recommend_workflow` first. If it returns a
   workflow, call `brain_get_workflow`; ask for confirmation for confidence
   0.50–0.79 and follow automatically at 0.80 or higher.
2. Call `brain_search` before answering questions about past work, decisions,
   debugging, repeated patterns, or prior projects. Use conservative search by
   default; use exploration or candidates only when the user asks for broad
   context or no useful result is found.
3. Call `brain_get` for the most relevant result before relying on its detail.
   For dates, use `brain_list_by_date` first; for exact topics or projects, use
   `brain_list_by_label` when useful.
4. For generic questions, prefer `pattern` results. Apply adapters or examples
   only when their provider, project, repository, organization, or role is
   explicit. Treat `abstained`, `degraded`, and `not_found` as meaningful; do
   not fill gaps from intuition.
5. Cite the returned Vault path and source references. If the MCP server is
   unavailable, say that memory was unavailable.
6. Call `brain_remember` only when the user explicitly asks to retain durable
   knowledge. The item is sanitized and saved automatically in the local Vault.
7. Feedback is part of the learning loop:
   - When the user explicitly judges search results, call
     `brain_record_search_feedback` with `relevant`, `partially_relevant`, or
     `irrelevant`, a concise reason, and useful tags such as `scope_mismatch`,
     `too_specific`, `useful_example`, or `useful_pattern`.
   - When the user approves, rejects, or reports execution of a recommended
     workflow, call `brain_record_feedback` with `approved`, `rejected`,
     `executed_success`, or `failed` and concise sanitized notes.
   - Do not record feedback for an ordinary search with no user judgment.
8. Use `brain_health` only to diagnose an unavailable Brain service. Never
   expose credentials, raw sessions, or full source bodies.

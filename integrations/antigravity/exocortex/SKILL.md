---
name: exocortex
description: Search and retrieve durable engineering knowledge, past architectures, repeated patterns, commands, debugging history, and proven workflows from the Exocortex long-term memory system via MCP.
---

# Exocortex Engineering Memory

Use the configured Exocortex MCP server as a durable, grounded neocortex for engineering workflows, past decisions, architecture context, and debugging patterns.

## Available MCP Tools

- `brain_recommend_workflow(task_description, space_id, ...)`: Recommend verified, confirmed procedures for common tasks.
- `brain_get_workflow(workflow_id)`: Retrieve full steps, validation gates, and evidence notes for a workflow.
- `brain_search(query, answer_mode, space_id, ...)`: Search past notes, decisions, and patterns using hybrid graph, full-text, and vector search.
- `brain_get(note_id)`: Retrieve the full canonical Markdown note with claims and structured facts.
- `brain_list_by_date(from_date, to_date)`: Chronological timeline of engineering sessions and work.
- `brain_list_by_label(label)`: Query notes linked by canonical taxonomy labels (e.g., `topic:gcp`, `topic:terraform`, `technology:python`).
- `brain_remember(content, space_id, title)`: Explicitly capture and sanitize a new durable fact, pattern, or decision requested by the user.
- `brain_record_search_feedback(query, note_id, outcome, ...)`: Record relevance feedback to continuously improve retrieval quality.

## Usage Guidelines

1. **Before Repeating Complex Tasks**:
   Call `brain_recommend_workflow` first. If a workflow is found with confidence >= 0.80, follow its validated steps. If confidence is between 0.50 and 0.79, propose it to the user for confirmation.
2. **When Answering Architecture / Prior Work Questions**:
   Call `brain_search` to ground your answers on past codebase decisions, repository structures, or prior incidents.
3. **Explicit Memory Capture**:
   Only call `brain_remember` when the user explicitly asks to remember, save, or bookmark a decision, pattern, or solution.
4. **Citation**:
   Always cite the note title or ID when presenting information retrieved from Exocortex.

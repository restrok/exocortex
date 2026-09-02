"""Tests for OpenAI-compatible gateway requests."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from exocortex.gateway import GatewayClient, GatewayError, _clean_json
from tests.conftest import make_settings


def test_gateway_extracts_json_with_mock_transport(tmp_path) -> None:
    """Gateway extraction uses structured output and returns validated knowledge."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "title": "Migration decision",
                                    "note_type": "decision",
                                    "summary": "Use a versioned migration.",
                                    "facts": ["Schema must be idempotent."],
                                    "links": [],
                                    "confidence": 0.9,
                                }
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = GatewayClient(make_settings(tmp_path / "brain"), client=client)

    result = gateway.extract("Sanitized migration notes.")

    assert result.title == "Migration decision"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert "<untrusted_source>" in captured["body"]["messages"][1]["content"]


def test_gateway_normalizes_non_string_scope_values(tmp_path) -> None:
    """A malformed scope does not discard an otherwise valid extraction."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "title": "GitLab remediation workflow",
                                    "note_type": "workflow",
                                    "summary": "Use GitLab as the source of truth.",
                                        "scope": {
                                        "organization": ["Acme Corp"],
                                        "provider": {"name": "GitLab"},
                                        "runtime": None,
                                        "repository": ["network", "security"],
                                        "confidence": "0.9",
                                    },
                                    "actions": [
                                        {
                                            "action_key": "open_merge_request",
                                            "route": {"name": "GitLab API"},
                                            "outcome": ["MR created"],
                                            "subjects": "repository",
                                            "tools": [{"label": "GitLab"}],
                                        }
                                    ],
                                }
                            )
                        }
                    }
                ]
            },
        )

    gateway = GatewayClient(
        make_settings(tmp_path / "brain"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = gateway.extract("A sanitized security workflow.")

    assert result.note_type == "workflow"
    assert result.scope.organization == "Acme Corp"
    assert result.scope.provider == "GitLab"
    assert result.scope.runtime == ""
    assert result.scope.repository == "network, security"
    assert result.scope.confidence == 0.9
    assert result.actions[0].route == "GitLab API"
    assert result.actions[0].outcome == "MR created"
    assert result.actions[0].subjects == ["repository"]
    assert result.actions[0].tools == ["GitLab"]


def test_gateway_extracts_a_structured_batch(tmp_path) -> None:
    """Batch extraction preserves source IDs and validates every item."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "items": [
                                        {
                                            "source_id": "source-1",
                                            "knowledge": {
                                                "title": "First source",
                                                "note_type": "task",
                                                "summary": "First summary.",
                                                "facts": [],
                                                "links": [],
                                                "claims": [],
                                                "confidence": 0.9,
                                                "labels": [],
                                                "evidence_status": "confirmed_success",
                                            },
                                        },
                                        {
                                            "source_id": "source-2",
                                            "knowledge": {
                                                "title": "Second source",
                                                "note_type": "task",
                                                "summary": "Second summary.",
                                                "facts": [],
                                                "links": [],
                                                "claims": [],
                                                "confidence": 0.9,
                                                "labels": [],
                                                "evidence_status": "confirmed_success",
                                            },
                                        },
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    gateway = GatewayClient(
        make_settings(tmp_path / "brain"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = gateway.extract_batch(
        [
            {"source_id": "source-1", "title": "First", "content": "one"},
            {"source_id": "source-2", "title": "Second", "content": "two"},
        ]
    )

    assert sorted(result) == ["source-1", "source-2"]
    assert result["source-2"].summary == "Second summary."
    assert captured["body"]["response_format"] == {"type": "json_object"}


def test_gateway_retries_transient_batch_status(tmp_path) -> None:
    """A transient gateway error gets one bounded retry."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(502)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "items": [
                                        {
                                            "source_id": "source-1",
                                            "knowledge": {
                                                "title": "Recovered source",
                                                "note_type": "task",
                                                "summary": "Recovered summary.",
                                                "facts": [],
                                                "links": [],
                                                "claims": [],
                                                "confidence": 0.9,
                                                "labels": [],
                                                "evidence_status": "confirmed_success",
                                            },
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    settings = make_settings(tmp_path / "brain")
    settings.gateway_retry_backoff_seconds = 0
    gateway = GatewayClient(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = gateway.extract_batch([{"source_id": "source-1", "content": "one"}])

    assert calls == 2
    assert result["source-1"].title == "Recovered source"


def test_gateway_interrupts_request_at_wall_clock_deadline(tmp_path) -> None:
    """A hanging transport cannot exceed the configured total request budget."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        time.sleep(2)
        return httpx.Response(200)

    settings = make_settings(tmp_path / "brain")
    settings.gateway_wall_timeout_seconds = 1
    gateway = GatewayClient(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    started = time.perf_counter()
    with pytest.raises(httpx.ReadTimeout, match="wall-clock"):
        gateway.extract_batch([{"source_id": "source-1", "content": "one"}])

    assert time.perf_counter() - started < 1.5


def test_gateway_keeps_valid_item_when_batch_repeats_an_id(
    tmp_path,
    caplog,
) -> None:
    """A duplicate item does not discard a valid sibling result."""

    def knowledge(title: str) -> dict[str, object]:
        return {
            "title": title,
            "note_type": "task",
            "summary": "Validated summary.",
            "facts": [],
            "links": [],
            "claims": [],
            "confidence": 0.9,
            "labels": [],
            "evidence_status": "confirmed_success",
        }

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "items": [
                                        {
                                            "source_id": "source-1",
                                            "knowledge": knowledge("First"),
                                        },
                                        {
                                            "source_id": "source-1",
                                            "knowledge": knowledge("Secret output"),
                                        },
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    gateway = GatewayClient(
        make_settings(tmp_path / "brain"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with caplog.at_level("INFO"):
        result = gateway.extract_batch(
            [
                {"source_id": "source-1", "content": "one"},
                {"source_id": "source-2", "content": "two"},
            ]
        )

    assert "status=200" in caplog.text
    assert "stage=duplicate_source_id" in caplog.text
    assert "Gateway batch contract partial" in caplog.text
    assert sorted(result) == ["source-1"]
    assert "Secret output" not in caplog.text


def test_gateway_keeps_valid_items_when_batch_is_short(tmp_path, caplog) -> None:
    """A short response returns valid items and leaves missing IDs to fallback."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "items": [
                                        {
                                            "source_id": "source-1",
                                            "knowledge": {
                                                "title": "First source",
                                                "summary": "First summary.",
                                                "claims": [],
                                            },
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    gateway = GatewayClient(
        make_settings(tmp_path / "brain"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with caplog.at_level("WARNING"):
        result = gateway.extract_batch(
            [
                {"source_id": "source-1", "content": "one"},
                {"source_id": "source-2", "content": "two"},
            ]
        )

    assert sorted(result) == ["source-1"]
    assert "expected_items=2 returned_items=1" in caplog.text


def test_gateway_rebinds_cross_source_evidence_only_when_fragment_matches(
    tmp_path,
    caplog,
) -> None:
    """Cross-source IDs are repaired only when the fragment belongs locally."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "items": [
                                        {
                                            "source_id": "source-1",
                                            "knowledge": {
                                                "title": "Deployment result",
                                                "summary": "The deployment passed.",
                                                "claims": [
                                                    {
                                                        "id": "claim-1",
                                                        "text": (
                                                            "The deployment passed."
                                                        ),
                                                        "claim_key": "deploy.passed",
                                                        "evidence": [
                                                            {
                                                                "source_id": "source-2",
                                                                "fragment": (
                                                                    "deployment passed"
                                                                ),
                                                            },
                                                                {
                                                                    "source_id": (
                                                                        "source-2"
                                                                    ),
                                                                    "fragment": (
                                                                        "not present"
                                                                    ),
                                                                },
                                                        ],
                                                    }
                                                ],
                                            },
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    gateway = GatewayClient(
        make_settings(tmp_path / "brain"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with caplog.at_level("WARNING"):
        result = gateway.extract_batch(
            [
                {
                    "source_id": "source-1",
                    "content": "The deployment passed successfully.",
                }
            ]
        )

    evidence = result["source-1"].claims[0].evidence
    assert len(evidence) == 1
    assert evidence[0].source_id == "source-1"
    assert "rebound=1 dropped=1" in caplog.text


def test_gateway_batch_requires_title_and_summary(tmp_path) -> None:
    """A batch item still needs enough text to create a useful note."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "items": [
                                        {
                                            "source_id": "source-1",
                                            "knowledge": {
                                            "facts": ["Missing title and summary."],
                                            },
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    gateway = GatewayClient(
        make_settings(tmp_path / "brain"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GatewayError, match="stage=knowledge_fields_missing"):
        gateway.extract_batch([{"source_id": "source-1", "content": "one"}])


def test_gateway_batch_normalizes_non_domain_vocabularies(tmp_path) -> None:
    """Batch output accepts safe aliases and degrades unknown values."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "items": [
                                        {
                                            "source_id": "source-1",
                                            "knowledge": {
                                                "title": "Invalid vocabulary",
                                                "note_type": "memo",
                                                "summary": "Must fail.",
                                                "facts": [],
                                                "links": [],
                                                "claims": [],
                                                "confidence": 0.9,
                                                "labels": [],
                                                "evidence_status": "verified",
                                            },
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    gateway = GatewayClient(
        make_settings(tmp_path / "brain"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = gateway.extract_batch([{"source_id": "source-1", "content": "one"}])

    assert result["source-1"].note_type == "task"
    assert result["source-1"].evidence_status == "confirmed_success"


def test_gateway_batch_normalizes_confidence_and_claim_variants(tmp_path) -> None:
    """One odd optional field does not invalidate a complete batch item."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "items": [
                                        {
                                            "source_id": "source-1",
                                            "knowledge": {
                                                "title": "Variant output",
                                                "summary": "A usable summary.",
                                                "confidence": "high",
                                                "claims": [
                                                    {
                                                        "id": "claim-1",
                                                        "text": "The change worked.",
                                                        "claim_key": "change.worked",
                                                        "claim_type": "observation",
                                                        "confidence": "medium",
                                                        "evidence": {
                                                            "fragment": (
                                                                "The change worked."
                                                            )
                                                        },
                                                    }
                                                ],
                                            },
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    gateway = GatewayClient(
        make_settings(tmp_path / "brain"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = gateway.extract_batch(
        [{"source_id": "source-1", "content": "one"}]
    )["source-1"]

    assert result.confidence == 0.5
    assert result.claims[0].confidence == 0.5
    assert result.claims[0].claim_type == "tool_observation"
    assert result.claims[0].evidence[0].source_id == "source-1"


def test_gateway_embeds_multiple_documents_in_one_request(tmp_path) -> None:
    """Embedding batches preserve response index order and request cardinality."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.3, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ]
            },
        )

    gateway = GatewayClient(
        make_settings(tmp_path / "brain"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert gateway.embed_batch(["first", "second"]) == [
        [0.1, 0.2],
        [0.3, 0.4],
    ]
    assert captured["input"] == ["first", "second"]


def test_gateway_normalizes_object_facts_and_links(tmp_path) -> None:
    """Gateway object variants do not interrupt a complete session ingestion."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "title": "Configuration review",
                                    "summary": "Use a profile-based configuration.",
                                    "facts": [
                                        {"fact": "The configuration is profile-based."}
                                    ],
                                    "links": [
                                        {
                                            "label": "Architecture document",
                                            "url": "https://example.test/architecture",
                                        }
                                    ],
                                }
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = GatewayClient(make_settings(tmp_path / "brain"), client=client)

    result = gateway.extract("Sanitized configuration notes.")

    assert result.facts == ["The configuration is profile-based."]
    assert result.links == ["Architecture document"]


def test_gateway_normalizes_claim_aliases_and_single_evidence(tmp_path) -> None:
    """Gateway claim variants are converted to the canonical schema-v2 values."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "title": "Gateway compatibility",
                                    "summary": "Normalize claims.",
                                    "claims": [
                                        {
                                            "id": "claim-1",
                                            "text": "Normalize claims.",
                                            "claim_key": "gateway.claims",
                                            "polarity": "positive",
                                            "claim_type": "observation",
                                            "evidence": {
                                                "source_id": "source-1",
                                                "fragment": "Normalize claims.",
                                            },
                                        }
                                    ],
                                }
                            )
                        }
                    }
                ]
            },
        )

    gateway = GatewayClient(
        make_settings(tmp_path / "brain"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = gateway.extract("Normalize claims.")

    assert result.claims[0].polarity == "affirmed"
    assert result.claims[0].claim_type == "tool_observation"
    assert len(result.claims[0].evidence) == 1


def test_gateway_downgrades_unknown_claim_types_safely(tmp_path) -> None:
    """Unknown model labels remain searchable without activating workflows."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "title": "Unknown claim",
                                    "summary": "Keep this conservative.",
                                    "claims": [
                                        {
                                            "id": "claim-unknown",
                                            "text": "Keep this conservative.",
                                            "claim_key": "unknown.claim",
                                            "claim_type": "new_gateway_label",
                                            "polarity": "unexpected",
                                            "evidence": [],
                                        }
                                    ],
                                }
                            )
                        }
                    }
                ]
            },
        )

    gateway = GatewayClient(
        make_settings(tmp_path / "brain"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = gateway.extract("Keep this conservative.")

    assert result.claims[0].claim_type == "assistant_suggestion"
    assert result.claims[0].polarity == "affirmed"


def test_gateway_downgrades_unknown_note_types_safely(tmp_path) -> None:
    """Unknown model note labels remain valid conservative task notes."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "title": "Unknown note",
                                    "note_type": "experience_log",
                                    "summary": "Keep this searchable.",
                                }
                            )
                        }
                    }
                ]
            },
        )

    gateway = GatewayClient(
        make_settings(tmp_path / "brain"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = gateway.extract("Keep this searchable.")

    assert result.note_type == "task"


def test_gateway_reflects_only_structured_workflow_proposals(tmp_path) -> None:
    """Reflection uses the configured reflection model and validates JSON."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "workflows": [
                                        {
                                            "title": "Terraform validation",
                                            "summary": "Validate before applying.",
                                            "triggers": ["Terraform change"],
                                            "steps": ["Run fmt", "Run plan"],
                                            "validation": [
                                                {
                                                    "text": "Plan succeeds",
                                                    "evidence_claim_ids": ["claim-1"],
                                                }
                                            ],
                                            "labels": ["terraform"],
                                            "action": {
                                                "action_key": "deploy.validate",
                                                "canonical_action_key": (
                                                    "repository.changes.validate"
                                                ),
                                                "tools": ["terraform"],
                                                "route": "terraform.skill",
                                                "confidence": 0.9,
                                            },
                                            "evidence_note_ids": ["note-1", "note-2"],
                                            "confidence": 0.9,
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    gateway = GatewayClient(
        make_settings(tmp_path / "brain"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = gateway.reflect("experience", "workflow")

    assert result.workflows[0].title == "Terraform validation"
    assert result.workflows[0].steps[0].text == "Run fmt"
    assert result.workflows[0].validation == ["Plan succeeds"]
    assert result.workflows[0].action.action_key == "deploy.validate"
    assert (
        result.workflows[0].action.canonical_action_key
        == "repository.changes.validate"
    )
    assert captured["body"]["model"] == "gpt-5.6-luna"
    assert captured["body"]["reasoning_effort"] == "high"
    assert "temperature" not in captured["body"]


def test_gateway_accepts_object_validation_from_reflection_model(tmp_path) -> None:
    """Reflection accepts validation objects with a checks list."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "workflows": [
                                        {
                                            "title": "Deploy safely",
                                            "summary": "Validate before applying.",
                                            "steps": [],
                                            "validation": {
                                                "checks": ["Plan succeeds"],
                                                "evidence_claim_ids": ["claim-1"],
                                            },
                                            "evidence_note_ids": ["note-1"],
                                            "confidence": 0.6,
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    gateway = GatewayClient(
        make_settings(tmp_path / "brain"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = gateway.reflect("experience", "workflow")

    assert result.workflows[0].validation == ["Plan succeeds"]


def test_gateway_health_embedding_headers_and_json_recovery(tmp_path) -> None:
    """Health and embeddings use the same bounded client without leaking keys."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"data": [{"embedding": [1, "2"]}]})

    settings = make_settings(tmp_path / "brain")
    gateway = GatewayClient(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert gateway.health() == {"data": []}
    assert gateway.embed("sanitized") == [1.0, 2.0]
    assert gateway._headers == {"Authorization": "Bearer test-key"}
    assert len(requests) == 2
    assert _clean_json('prefix {"ok": true} suffix') == '{"ok": true}'


def test_gateway_rejects_invalid_payloads(tmp_path) -> None:
    """Malformed model output becomes a non-sensitive domain error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not-json"}}]},
        )

    gateway = GatewayClient(
        make_settings(tmp_path / "brain"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(GatewayError, match="JSON"):
        gateway.extract("sanitized")
    with pytest.raises(GatewayError):
        _clean_json("no object")

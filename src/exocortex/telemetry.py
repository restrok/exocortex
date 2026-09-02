"""Small, optional OpenTelemetry surface for Codex Brain."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from typing import Any, TypeVar, cast

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Counter, Histogram
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

from exocortex import __version__
from exocortex.config import Settings

_LOGGER = logging.getLogger(__name__)
_TELEMETRY_CONFIGURED = False
_INSTRUMENTS: _MetricInstruments | None = None
F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class _MetricInstruments:
    """Metrics emitted without user content or gateway response bodies."""

    gateway_requests: Counter
    gateway_duration: Histogram
    ingest_records: Counter
    ingest_llm_calls: Counter
    sync_notes: Counter
    reflect_notes: Counter
    reflect_workflows: Counter


def configure_telemetry(settings: Settings) -> None:
    """Configure OTLP exporters once when telemetry is explicitly enabled."""
    global _TELEMETRY_CONFIGURED
    if _TELEMETRY_CONFIGURED or not settings.otel_enabled:
        return

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": __version__,
            "deployment.environment.name": settings.otel_environment,
        }
    )
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=settings.otel_exporter_endpoint,
                insecure=settings.otel_exporter_insecure,
            ),
            schedule_delay_millis=settings.otel_span_export_delay_ms,
        )
    )
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(
            endpoint=settings.otel_exporter_endpoint,
            insecure=settings.otel_exporter_insecure,
        ),
        export_interval_millis=settings.otel_metric_export_interval_ms,
    )
    metrics.set_meter_provider(
        MeterProvider(resource=resource, metric_readers=[metric_reader])
    )
    _TELEMETRY_CONFIGURED = True
    _LOGGER.info(
        "OpenTelemetry enabled service=%s endpoint=%s",
        settings.otel_service_name,
        settings.otel_exporter_endpoint,
    )


def flush_telemetry() -> None:
    """Flush short-lived CLI telemetry without requiring OTEL to be enabled."""
    if not _TELEMETRY_CONFIGURED:
        return
    tracer_provider = trace.get_tracer_provider()
    if isinstance(tracer_provider, TracerProvider):
        tracer_provider.force_flush()
    meter_provider = metrics.get_meter_provider()
    if isinstance(meter_provider, MeterProvider):
        meter_provider.force_flush()


@contextmanager
def operation_span(
    name: str,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Span]:
    """Create a span and record only bounded error metadata."""
    tracer = trace.get_tracer("exocortex")
    with tracer.start_as_current_span(name, attributes=attributes) as span:
        try:
            yield span
        except Exception as error:  # pylint: disable=broad-except
            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("error.type", error.__class__.__name__)
            raise


def traced(name: str) -> Callable[[F], F]:
    """Decorate a method with a bounded operation span."""

    def decorator(function: F) -> F:
        @wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with operation_span(name):
                return function(*args, **kwargs)

        return cast(F, wrapper)

    return decorator


def record_gateway_request(
    operation: str,
    status: str,
    duration_seconds: float,
) -> None:
    """Record one gateway request with no URL, prompt, or response content."""
    instruments = _metric_instruments()
    attributes = {
        "brain.gateway.operation": operation,
        "brain.gateway.status": status,
    }
    instruments.gateway_requests.add(1, attributes)
    instruments.gateway_duration.record(duration_seconds, attributes)


def record_ingest_summary(summary: dict[str, object]) -> None:
    """Record bounded ingestion counters from one completed cycle."""
    instruments = _metric_instruments()
    for outcome, field in (
        ("extracted", "extracted"),
        ("fallback", "fallback"),
        ("failed", "records_failed"),
        ("trivial", "trivial_skipped"),
        ("unchanged", "records_unchanged"),
    ):
        count = _as_int(summary.get(field))
        if count:
            instruments.ingest_records.add(count, {"brain.ingest.outcome": outcome})
    llm_calls = _as_int(summary.get("llm_calls"))
    if llm_calls:
        instruments.ingest_llm_calls.add(llm_calls)


def record_sync(count: int, embed: bool) -> None:
    """Record the number of notes projected by sync."""
    if count:
        _metric_instruments().sync_notes.add(
            count,
            {"brain.sync.embed": str(embed).lower()},
        )


def record_reflection(processed: int, workflows: int) -> None:
    """Record reflection throughput and accepted workflow count."""
    instruments = _metric_instruments()
    if processed:
        instruments.reflect_notes.add(processed)
    if workflows:
        instruments.reflect_workflows.add(workflows)


def _metric_instruments() -> _MetricInstruments:
    """Create metric instruments lazily for both SDK and no-op providers."""
    global _INSTRUMENTS
    if _INSTRUMENTS is None:
        meter = metrics.get_meter("exocortex")
        _INSTRUMENTS = _MetricInstruments(
            gateway_requests=meter.create_counter(
                "exocortex_gateway_requests",
                description="Gateway requests issued by Codex Brain.",
            ),
            gateway_duration=meter.create_histogram(
                "exocortex_gateway_request_duration_seconds",
                unit="s",
                description="Gateway request duration in seconds.",
            ),
            ingest_records=meter.create_counter(
                "exocortex_ingest_records",
                description="Ingest records by outcome.",
            ),
            ingest_llm_calls=meter.create_counter(
                "exocortex_ingest_llm_calls",
                description="LLM extraction calls issued by ingestion.",
            ),
            sync_notes=meter.create_counter(
                "exocortex_sync_notes",
                description="Notes projected by sync.",
            ),
            reflect_notes=meter.create_counter(
                "exocortex_reflect_notes",
                description="Notes considered by reflection.",
            ),
            reflect_workflows=meter.create_counter(
                "exocortex_reflect_workflows",
                description="Workflows accepted by reflection.",
            ),
        )
    return _INSTRUMENTS


def _as_int(value: object) -> int:
    """Convert persisted summary values to a safe non-negative integer."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def monotonic_seconds() -> float:
    """Return a monotonic timestamp for request duration measurement."""
    return time.perf_counter()

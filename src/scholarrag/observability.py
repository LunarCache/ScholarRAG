from __future__ import annotations

from threading import Lock

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from scholarrag.config import Settings

_lock = Lock()
_configured = False


def configure_telemetry(settings: Settings) -> None:
    global _configured
    with _lock:
        if _configured:
            return
        provider = TracerProvider(
            resource=Resource.create({"service.name": settings.otel_service_name})
        )
        if settings.phoenix_endpoint:
            exporter = OTLPSpanExporter(endpoint=settings.phoenix_endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _configured = True


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("scholarrag")


def current_trace_id() -> str:
    context = trace.get_current_span().get_span_context()
    return f"{context.trace_id:032x}" if context.is_valid else ""

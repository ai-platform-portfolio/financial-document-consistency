"""Metadata-only tracing to the platform collector; document text stays out of logs."""

import json
import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter


def configure():
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": os.environ.get(
                        "OTEL_SERVICE_NAME", "document-pipeline"
                    )
                }
            )
        )
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(timeout=3)))
        trace.set_tracer_provider(provider)
    logging.basicConfig(level=logging.INFO, format="%(message)s")


def event(name, **metadata):
    logging.getLogger("pipeline").info(
        json.dumps({"event": name, **metadata}, default=str)
    )


tracer = trace.get_tracer("document-pipeline")

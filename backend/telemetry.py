from __future__ import annotations

import os
import re
import time

from fastapi import FastAPI, Request
from fastapi.responses import Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest, start_http_server

HTTP_REQUESTS = Counter("research_http_requests_total", "HTTP-запросы", ["method", "path", "status"])
HTTP_LATENCY = Histogram("research_http_request_seconds", "Длительность HTTP-запроса", ["method", "path"])
MODEL_TOKENS = Counter("research_model_tokens_total", "Токены модели", ["model", "direction"])
MODEL_LATENCY = Histogram("research_model_request_seconds", "Длительность вызова модели", ["model"])
RESEARCH_PHASES = Counter("research_phases_total", "Этапы исследования", ["phase"])


def configure_tracing(service_name: str) -> None:
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not endpoint:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=endpoint.startswith("http://"))))
    trace.set_tracer_provider(provider)


def instrument_api(app: FastAPI) -> None:
    @app.middleware("http")
    async def measure(request: Request, call_next):
        route = request.scope.get("route")
        route_path = route.path if route else re.sub(
            r"/[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", "/{id}", request.url.path,
        )
        started = time.monotonic()
        with trace.get_tracer("research.api").start_as_current_span(
            "http.request", record_exception=False, set_status_on_exception=False,
        ) as span:
            span.set_attribute("http.method", request.method)
            span.set_attribute("http.route", route_path)
            response = await call_next(request)
            span.set_attribute("http.status_code", response.status_code)
        HTTP_REQUESTS.labels(request.method, route_path, str(response.status_code)).inc()
        HTTP_LATENCY.labels(request.method, route_path).observe(time.monotonic() - started)
        return response

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def worker_metrics_server() -> None:
    start_http_server(int(os.getenv("WORKER_METRICS_PORT", "9091")),
                      addr=os.getenv("WORKER_METRICS_HOST", "0.0.0.0"))

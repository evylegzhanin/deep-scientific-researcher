from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest
import yaml
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from backend.config import Settings
from backend.models import ModelGateway
from backend.research import ResearchEngine


@pytest.mark.asyncio
async def test_agent_and_model_spans_have_no_prompt_or_response(monkeypatch) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)
    secret = "Секретное давление 99 бар, private@example.org"

    async def retrieve(state):
        return {"evidence": [{"text": secret}]}

    settings = Settings(_env_file=None)
    gateway = ModelGateway(settings)

    class FakeChat:
        async def ainvoke(self, messages):
            return SimpleNamespace(content=secret, usage_metadata={"input_tokens": 12, "output_tokens": 7})

    gateway.text = FakeChat()
    with provider.get_tracer("test").start_as_current_span("research.run"):
        await ResearchEngine._traced(None, "retrieve", retrieve)({"research_id": "safe-id"})
        assert await gateway.generate(secret, secret) == secret
    spans = exporter.get_finished_spans()
    assert {span.name for span in spans} == {"research.run", "research.retrieve", "model.text"}
    assert all(span.context.trace_id == spans[0].context.trace_id for span in spans)
    assert all(secret not in str(dict(span.attributes)) for span in spans)
    model_span = next(span for span in spans if span.name == "model.text")
    assert model_span.attributes["langfuse.observation.type"] == "generation"
    assert model_span.attributes["gen_ai.usage.input_tokens"] == 12
    assert model_span.attributes["gen_ai.usage.output_tokens"] == 7
    provider.shutdown()


def test_langfuse_export_is_local_and_attributes_fail_closed() -> None:
    config = yaml.safe_load(Path("infra/otel-collector.langfuse.yaml").read_text(encoding="utf-8"))
    redaction = config["processors"]["redaction/langfuse"]
    assert redaction["allow_all_keys"] is False
    forbidden = {
        "langfuse.observation.input", "langfuse.observation.output",
        "gen_ai.prompt", "gen_ai.completion", "input.value", "output.value",
    }
    assert forbidden.isdisjoint(redaction["allowed_keys"])
    exporter = config["exporters"]["otlphttp/langfuse"]
    assert exporter["endpoint"] == "http://langfuse-web:3000/api/public/otel"
    assert exporter["headers"]["x-langfuse-ingestion-version"] == "4"
    pipeline = config["service"]["pipelines"]["traces/langfuse"]
    assert pipeline["processors"][:2] == ["redaction/langfuse", "transform/langfuse"]

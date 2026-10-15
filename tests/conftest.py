from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.audience import AudienceEngine
from app.catalog import load_catalog
from app.llm import LLMResult, ProviderUnavailable
from app.main import create_app
from app.settings import ROOT, Settings
from app.synthdata import generate


class ScriptedProvider:
    """Stands in for the model: returns queued proposals (or raises) and records what it was sent."""

    name = "scripted"
    model = "scripted-model"

    def __init__(self, *responses: dict[str, Any] | Exception):
        self.responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []
        self.schemas: list[dict[str, Any] | None] = []

    def propose(self, system: str, messages: list[dict[str, Any]], schema: dict[str, Any] | None = None) -> LLMResult:
        self.calls.append(messages)
        self.schemas.append(schema)
        if not self.responses:
            raise ProviderUnavailable("script exhausted")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return LLMResult(proposal=response, model=self.model, latency_ms=5.0, input_tokens=100, output_tokens=20)


def proposal(conditions=None, terms=None, unmapped=None, channel=None, out_of_scope=False, excluded=None) -> dict[str, Any]:
    return {
        "conditions": conditions or [],
        "glossary_terms": terms or [],
        "excluded_terms": excluded or [],
        "unmapped_phrases": unmapped or [],
        "channel": channel,
        "out_of_scope": out_of_scope,
        "assumptions": [],
    }


@pytest.fixture(scope="session")
def data_path(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("data") / "profiles.duckdb"
    generate(path, 20_000)
    return path


@pytest.fixture(scope="session")
def catalog():
    return load_catalog(ROOT / "config")


@pytest.fixture(scope="session")
def engine(catalog, data_path) -> AudienceEngine:
    return AudienceEngine(data_path, catalog, min_audience=100)


@pytest.fixture
def settings(tmp_path, data_path) -> Settings:
    return Settings(llm_provider="test", data_path=data_path, audit_db_path=tmp_path / "audit.sqlite",
                    fixtures_dir=tmp_path / "fixtures")


@pytest.fixture
def make_client(settings):
    clients = []

    def _make(provider, **overrides) -> TestClient:
        client = TestClient(create_app(replace(settings, **overrides), provider))
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)

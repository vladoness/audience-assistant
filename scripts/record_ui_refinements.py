"""Records live refinement responses for the UI's example requests, so the suggested changes also
work in offline mode. The first step replays the recorded interpretation (exactly what the offline
UI shows); only the refinement calls the live model.

    uv run python -m scripts.record_ui_refinements      # needs ANTHROPIC_API_KEY; default model + effort
"""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from app.llm import REFINE_SCHEMA, AnthropicProvider, RecordingProvider, ReplayProvider
from app.main import EXAMPLES, REFINE_EXAMPLES, create_app
from app.settings import Settings


class ReplayThenRecord:
    """Replays interpretations, records refinements."""

    name = "anthropic"

    def __init__(self, replay: ReplayProvider, recorder: RecordingProvider):
        self._replay, self._recorder = replay, recorder
        self.model, self.effort = recorder.model, recorder.effort

    def propose(self, system, messages, schema=None):
        if schema is REFINE_SCHEMA:
            return self._recorder.propose(system, messages, schema)
        return self._replay.propose(system, messages, schema)


def main() -> None:
    settings = Settings.from_env()
    effort = settings.anthropic_effort or None
    live = AnthropicProvider(settings.anthropic_api_key, settings.anthropic_model, settings.llm_timeout_s,
                             settings.llm_max_retries, effort=effort)
    provider = ReplayThenRecord(ReplayProvider(settings.fixtures_dir, settings.anthropic_model, effort),
                                RecordingProvider(live, settings.fixtures_dir))
    app = create_app(replace(settings, audit_db_path=settings.audit_db_path.with_name("record-ui.sqlite"),
                             rate_limit_per_min=10_000), provider)
    with TestClient(app) as client:
        for request in EXAMPLES:
            r = client.post("/api/interpret", json={"request": request}).json()
            for change in REFINE_EXAMPLES:
                response = client.post("/api/refine", json={"request_id": r["request_id"], "instruction": change,
                                                            "definition": r["definition"]})
                body = response.json()
                print(f"{response.status_code} {body.get('status', '-'):<20} {request[:50]:<50} + {change!r}  "
                      f"{body.get('changes')}")


if __name__ == "__main__":
    main()

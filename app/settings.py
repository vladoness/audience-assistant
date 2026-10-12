"""Runtime configuration, read from environment variables (and a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    llm_provider: str = "replay"  # anthropic | replay | offline
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    anthropic_effort: str = "low"  # low | medium | high | xhigh | max | "" for the model default
    llm_timeout_s: float = 10.0
    llm_max_retries: int = 1
    breaker_threshold: int = 3
    breaker_cooldown_s: float = 30.0
    synthetic_data: bool = False
    record_fixtures: bool = False
    config_dir: Path = ROOT / "config"
    data_path: Path = ROOT / "data" / "profiles.duckdb"
    profile_rows: int = 200_000
    audit_db_path: Path = ROOT / "data" / "audit.sqlite"
    fixtures_dir: Path = ROOT / "evals" / "fixtures"
    cache_ttl_s: int = 3600
    cache_max_entries: int = 1000
    max_request_chars: int = 500
    min_audience: int = 100
    broad_share: float = 0.5
    rate_limit_per_min: int = 20
    daily_budget_usd: float = 5.0

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ROOT / ".env")
        key = os.getenv("ANTHROPIC_API_KEY", "")
        provider = os.getenv("LLM_PROVIDER") or ("anthropic" if key else "replay")
        return cls(
            llm_provider=provider,
            anthropic_api_key=key,
            anthropic_model=os.getenv("ANTHROPIC_MODEL", cls.anthropic_model),
            anthropic_effort=os.getenv("ANTHROPIC_EFFORT", cls.anthropic_effort),
            llm_timeout_s=float(os.getenv("LLM_TIMEOUT_S", cls.llm_timeout_s)),
            llm_max_retries=int(os.getenv("LLM_MAX_RETRIES", cls.llm_max_retries)),
            record_fixtures=os.getenv("RECORD_FIXTURES", "0") == "1",
            synthetic_data=os.getenv("SYNTHETIC_DATA", "0") == "1",
            data_path=Path(os.getenv("DATA_PATH", cls.data_path)),
            profile_rows=int(os.getenv("PROFILE_ROWS", cls.profile_rows)),
            audit_db_path=Path(os.getenv("AUDIT_DB_PATH", cls.audit_db_path)),
            cache_ttl_s=int(os.getenv("CACHE_TTL_S", cls.cache_ttl_s)),
            min_audience=int(os.getenv("MIN_AUDIENCE", cls.min_audience)),
            rate_limit_per_min=int(os.getenv("RATE_LIMIT_PER_MIN", cls.rate_limit_per_min)),
            daily_budget_usd=float(os.getenv("DAILY_BUDGET_USD", cls.daily_budget_usd)),
        )

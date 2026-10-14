"""Request/response shapes shared by the API, the interpreter and the evals."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Op = Literal["eq", "neq", "in", "not_in", "gt", "gte", "lt", "lte", "between"]

Status = Literal[
    "ready",  # valid, nothing pending: can be sized and approved
    "needs_clarification",  # an open glossary term needs the user's choice
    "needs_confirmation",  # parts of the request could not be mapped; user must accept dropping them
    "unsupported",  # nothing in the request is an audience the catalog can express
    "invalid",  # the proposal broke catalog rules even after one repair attempt
    "ai_unavailable",  # model call failed; the manual builder still works
]


class Condition(BaseModel):
    attribute: str
    op: Op
    value: Any
    source: str = Field("model", max_length=80)  # model | user | glossary:<term> | glossary:<term>=<option>


class AudienceDefinition(BaseModel):
    conditions: list[Condition] = Field(default_factory=list, max_length=30)
    channel: Literal["sms", "email"] = "sms"


class Issue(BaseModel):
    level: Literal["error", "warning"]
    code: str
    message: str
    attribute: str | None = None


class ClarificationOption(BaseModel):
    id: str
    label: str


class Clarification(BaseModel):
    term: str
    question: str
    options: list[ClarificationOption]


class Explanation(BaseModel):
    summary: str
    lines: list[str]
    governed_terms: list[str]


class Telemetry(BaseModel):
    provider: str
    origin: str = "ui"  # ui | mcp: who asked (the web UI, or an AI agent through the MCP server)
    model: str | None = None
    prompt_version: str | None = None
    llm_calls: int = 0
    repaired: bool = False
    cache_hit: bool = False
    pii_masked: int = 0
    llm_ms: float = 0.0
    total_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0


class Changes(BaseModel):
    """What a refinement changed, in plain English, so the marketer sees exactly what moved."""

    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    channel: str | None = None


class InterpretResult(BaseModel):
    request_id: str
    status: Status
    definition: AudienceDefinition
    clarifications: list[Clarification] = Field(default_factory=list)
    unmapped_phrases: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    explanation: Explanation | None = None
    telemetry: Telemetry
    parent_request_id: str | None = None  # set when this result refines an earlier interpretation
    changes: Changes | None = None
    request_text: str | None = None  # the masked request; set only when an interpretation is loaded for review


class SizeResult(BaseModel):
    base: int
    matched: int
    reachable: int
    excluded_no_consent: int
    share_of_base: float
    by_line_type: dict[str, int]
    by_region: dict[str, int]
    warnings: list[Issue]
    query_ms: float


class InterpretRequest(BaseModel):
    request: str = Field(max_length=2000)


class RefineRequest(BaseModel):
    request_id: str  # the interpretation being refined
    instruction: str = Field(max_length=2000)
    definition: AudienceDefinition  # what is on screen now, including the marketer's edits


class CheckRequest(BaseModel):
    definition: AudienceDefinition
    pending_terms: list[str] = Field(default_factory=list)
    resolutions: dict[str, str] = Field(default_factory=dict)


class CheckResult(BaseModel):
    status: Status
    definition: AudienceDefinition
    clarifications: list[Clarification]
    issues: list[Issue]
    explanation: Explanation


class ApproveRequest(BaseModel):
    request_id: str
    name: str = Field(min_length=3, max_length=80)
    approver: str = Field(min_length=2, max_length=60)
    definition: AudienceDefinition
    acknowledged_unmapped: bool = False

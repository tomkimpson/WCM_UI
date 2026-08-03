"""The wire contract.

This module is what Stage 4's frontend codegens against, so it is deliberately
logic-free: naming and types only. Freezing it early means the frontend can
start before the endpoints are finished.

Two invariants have tests of their own rather than just comments:

  - No response model carries a client IP, hashed or otherwise. IP-derived data
    lives only in the per-day quota counters, which expire on a TTL.
  - ``field_order`` covers exactly the schema's knobs, so a knob added to the
    schema cannot be silently invisible in the form.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

RunState = Literal["queued", "running", "succeeded", "failed"]
FailureSource = Literal["params", "worker", "postprocess", "infrastructure"]


class SubmitRequest(BaseModel):
    # extra="forbid" so a typo'd top-level key is a 422 rather than being
    # silently dropped — a user who mistypes "parameters" should be told.
    model_config = ConfigDict(extra="forbid")

    params: dict[str, Any] = Field(default_factory=dict)
    # `yaml_override`, not the doc's `optional_yaml_override`: "optional" is
    # type information, and it's already in the type.
    yaml_override: Optional[str] = Field(default=None, max_length=65_536)


class FieldError(BaseModel):
    path: str                       # "simulation.length_sec"
    pointer: str                    # "/simulation/length_sec" (RFC 6901)
    kind: str
    message: str
    keyword: Optional[str] = None
    constraint: Any = None
    line: Optional[int] = None      # yaml_syntax only
    column: Optional[int] = None    # yaml_syntax only

    @classmethod
    def from_data(cls, data) -> "FieldError":
        return cls(
            path=data.path, pointer=data.pointer, kind=data.kind,
            message=data.message, keyword=data.keyword,
            constraint=data.constraint, line=data.line, column=data.column,
        )


class ValidationResponse(BaseModel):
    """The /validate response. Always 200 — `valid` carries the verdict."""

    valid: bool
    errors: list[FieldError] = Field(default_factory=list)
    resolved_params: Optional[dict[str, Any]] = None
    # Present when valid, so the UI can say "you have already run this".
    content_hash: Optional[str] = None
    deterministic: Optional[bool] = None


class ErrorResponse(BaseModel):
    """The envelope for every 4xx and 5xx."""

    error: str                      # machine-readable slug
    message: str
    errors: Optional[list[FieldError]] = None
    retry_after_sec: Optional[int] = None
    request_id: Optional[str] = None


class RunCreatedResponse(BaseModel):
    run_id: str
    state: Literal["queued"]
    resolved_params: dict[str, Any]
    content_hash: Optional[str] = None
    deterministic: Optional[bool] = None
    image_uri: Optional[str] = None
    poll_after_ms: int


class ArtifactInfo(BaseModel):
    available: bool
    #: A relative API path, never a signed URL — those expire, and a response
    #: body may be cached or shared.
    url: Optional[str] = None
    expires_at: Optional[datetime] = None
    #: not_ready | expired | never_produced — lets the frontend render the right
    #: disabled state without probing GCS.
    reason: Optional[str] = None


class RunArtifacts(BaseModel):
    timeseries: ArtifactInfo
    tarball: ArtifactInfo
    params: ArtifactInfo
    stderr: ArtifactInfo


class RunProvenance(BaseModel):
    image_uri: Optional[str] = None
    image_digest: Optional[str] = None
    image_pin_source: Optional[str] = None
    wcecoli_git_sha: Optional[str] = None
    wcm_ui_git_sha: Optional[str] = None
    content_hash: Optional[str] = None
    hash_version: Optional[int] = None
    #: False when parca_cpus > 1 — multiprocess ParCa can reorder float
    #: reductions, so bit-identity is not claimable.
    deterministic: Optional[bool] = None
    #: False when any of digest or the SHAs is unresolved.
    complete: bool = False
    reproduce_command: Optional[str] = None


class RunDetail(BaseModel):
    run_id: str
    state: RunState
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_sec: Optional[float] = None

    #: What the simulation actually ran with.
    params: dict[str, Any] = Field(default_factory=dict)
    #: What the user typed, so "fork these parameters" reproduces their form.
    submitted_params: Optional[dict[str, Any]] = None
    #: Raw YAML. NEVER render as HTML.
    yaml_override: Optional[str] = None

    error_message: Optional[str] = None
    failure_source: Optional[FailureSource] = None
    attempt: Optional[int] = None

    provenance: RunProvenance
    artifacts: RunArtifacts

    #: Server-controlled poll cadence; None once terminal. Keeping this in the
    #: response means the interval can be widened without a frontend deploy.
    poll_after_ms: Optional[int] = None
    schema_version: Optional[int] = None


class SchemaResponse(BaseModel):
    """What the form is built from."""

    schema_version: int
    # Named strict_schema, not schema: the latter shadows BaseModel.schema and
    # warns in pydantic v2.
    strict_schema: dict[str, Any]
    loose_schema: dict[str, Any]
    defaults: dict[str, Any]
    #: JSON object key order is not a contract, so ordering is explicit.
    field_order: list[str]
    supported_namespaces: list[str]


class ConfigResponse(BaseModel):
    """Everything the frontend needs to render limits without hardcoding them."""

    accepting_runs: bool
    message: str = ""
    max_concurrent_runs: int
    max_runs_per_day: int
    max_runs_per_ip_per_day: int
    wall_clock_cap_sec: int
    tarball_retention_days: int
    signed_url_ttl_sec: int
    api_version: str


class HealthResponse(BaseModel):
    status: Literal["ok"]
    api_version: str
    revision: Optional[str] = None
    wcm_ui_git_sha: Optional[str] = None

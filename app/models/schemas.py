from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class QueryType(StrEnum):
    USERNAME = "username"
    EMAIL = "email"
    DOMAIN = "domain"
    NAME = "name"
    PHONE = "phone"


class FindingStatus(StrEnum):
    FOUND = "found"
    LIKELY = "likely"
    UNKNOWN = "unknown"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class LocationEvidenceType(StrEnum):
    DECLARED = "declared"
    REVIEW = "review"
    FITNESS_ACTIVITY = "fitness_activity"
    EVENT = "event"
    PHOTO = "photo"
    TEXT_MENTION = "text_mention"


class LocationObservationInput(BaseModel):
    place_name: str = Field(min_length=1, max_length=300)
    locality: str | None = Field(None, max_length=200)
    region: str | None = Field(None, max_length=200)
    country: str | None = Field(None, max_length=100)
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    precision_radius_m: int | None = Field(None, ge=0, le=1_000_000)
    evidence_type: LocationEvidenceType
    occurred_at: datetime | None = None
    published_at: datetime | None = None
    source_platform: str = Field(min_length=1, max_length=100)
    source_url: str | None = Field(None, max_length=2048)
    account_identifier: str | None = Field(None, max_length=320)
    privacy_masked: bool = False
    confidence: float | None = Field(None, ge=0, le=1)
    raw_evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("place_name", "locality", "region", "country", "source_platform")
    @classmethod
    def strip_location_text(cls, value: str | None) -> str | None:
        return value.strip() if value else value


class ProfileEvidence(BaseModel):
    avatar_url: str | None = None
    display_name: str | None = None
    biography: str | None = None
    location: str | None = None
    account_id: str | None = None
    verified: bool | None = None
    followers: int | None = Field(None, ge=0)
    following: int | None = Field(None, ge=0)


class FindingRecord(BaseModel):
    query_type: QueryType
    queried_value: str
    finding_type: str
    platform: str | None = None
    value: str | None = None
    canonical_url: str | None = None
    display_url: str | None = None
    status: FindingStatus
    confidence: float | None = Field(None, ge=0, le=1)
    title: str | None = None
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    profile: ProfileEvidence = Field(default_factory=ProfileEvidence)
    source_tool: str
    source_record_id: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    raw_output_reference: str | None = None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=320)
    query_type: QueryType
    title: str | None = Field(None, max_length=200)
    case_reference: str | None = Field(None, max_length=100)
    notes: str | None = Field(None, max_length=10000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    tools: list[str] = Field(default_factory=list, max_length=16)
    timeouts: dict[str, int] = Field(default_factory=dict)
    proxy: str | None = None
    auto_follow_username_pivots: bool = False
    extended_username_search: bool = False

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if any(ord(ch) < 32 for ch in value):
            raise ValueError("control characters are not allowed")
        return value.strip()

    @model_validator(mode="after")
    def require_compatible_collectors(self) -> "SearchRequest":
        if not self.tools:
            raise ValueError("at least one compatible collection tool is required")
        return self

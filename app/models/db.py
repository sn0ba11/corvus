from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def uuid_string() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Investigation(Base):
    __tablename__ = "investigations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    title: Mapped[str] = mapped_column(String(200))
    query: Mapped[str] = mapped_column(String(320))
    query_type: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    case_reference: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    selected_tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    proxy_name: Mapped[str | None] = mapped_column(String(20))
    auto_follow_username_pivots: Mapped[bool] = mapped_column(default=False)
    extended_username_search: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    runs: Mapped[list["ToolRun"]] = relationship(
        cascade="all, delete-orphan", back_populates="investigation"
    )
    findings: Mapped[list["Finding"]] = relationship(
        cascade="all, delete-orphan", back_populates="investigation"
    )
    location_observations: Mapped[list["LocationObservation"]] = relationship(
        cascade="all, delete-orphan", back_populates="investigation"
    )
    browser_runs: Mapped[list["BrowserRun"]] = relationship(
        cascade="all, delete-orphan", back_populates="investigation"
    )


class ToolRun(Base):
    __tablename__ = "tool_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    investigation_id: Mapped[str] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), index=True
    )
    tool_id: Mapped[str] = mapped_column(String(50))
    display_name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    tool_version: Mapped[str | None] = mapped_column(String(100))
    tool_commit: Mapped[str | None] = mapped_column(String(64))
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    query_value: Mapped[str | None] = mapped_column(String(320))
    query_type: Mapped[str | None] = mapped_column(String(20))
    pivot_depth: Mapped[int] = mapped_column(Integer, default=0)
    pivot_source_finding_id: Mapped[str | None] = mapped_column(String(36))
    pivot_automatic: Mapped[bool] = mapped_column(default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    rate_limited: Mapped[bool] = mapped_column(default=False)
    raw_output_reference: Mapped[str | None] = mapped_column(Text)
    investigation: Mapped[Investigation] = relationship(back_populates="runs")
    findings: Mapped[list["Finding"]] = relationship(
        cascade="all, delete-orphan", back_populates="tool_run"
    )


class Finding(Base):
    __tablename__ = "findings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    investigation_id: Mapped[str] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), index=True
    )
    tool_run_id: Mapped[str] = mapped_column(
        ForeignKey("tool_runs.id", ondelete="CASCADE"), index=True
    )
    query_type: Mapped[str] = mapped_column(String(20))
    queried_value: Mapped[str] = mapped_column(String(320))
    finding_type: Mapped[str] = mapped_column(String(50))
    platform: Mapped[str | None] = mapped_column(String(200))
    value: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text, index=True)
    display_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30))
    confidence: Mapped[float | None] = mapped_column(Float)
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    source_tool: Mapped[str] = mapped_column(String(50))
    source_tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_record_id: Mapped[str | None] = mapped_column(String(300))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    raw_output_reference: Mapped[str | None] = mapped_column(Text)
    investigation: Mapped[Investigation] = relationship(back_populates="findings")
    tool_run: Mapped[ToolRun] = relationship(back_populates="findings")


class LocationObservation(Base):
    __tablename__ = "location_observations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    investigation_id: Mapped[str] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), index=True
    )
    finding_id: Mapped[str | None] = mapped_column(
        ForeignKey("findings.id", ondelete="SET NULL"), index=True
    )
    place_name: Mapped[str] = mapped_column(String(300))
    locality: Mapped[str | None] = mapped_column(String(200))
    region: Mapped[str | None] = mapped_column(String(200))
    country: Mapped[str | None] = mapped_column(String(100))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    precision_radius_m: Mapped[int | None] = mapped_column(Integer)
    evidence_type: Mapped[str] = mapped_column(String(40))
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    last_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    source_platform: Mapped[str] = mapped_column(String(100))
    source_url: Mapped[str | None] = mapped_column(Text)
    account_identifier: Mapped[str | None] = mapped_column(String(320))
    privacy_masked: Mapped[bool] = mapped_column(default=False)
    confidence: Mapped[float] = mapped_column(Float)
    raw_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    investigation: Mapped[Investigation] = relationship(back_populates="location_observations")


class BrowserRun(Base):
    __tablename__ = "browser_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    investigation_id: Mapped[str] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), index=True
    )
    collector_id: Mapped[str] = mapped_column(String(50), default="generic_capture")
    session_name: Mapped[str] = mapped_column(String(100), default="sock-default")
    requested_url: Mapped[str] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    page_title: Mapped[str | None] = mapped_column(Text)
    challenge_type: Mapped[str | None] = mapped_column(String(100))
    browser_version: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    screenshot_reference: Mapped[str | None] = mapped_column(Text)
    screenshot_sha256: Mapped[str | None] = mapped_column(String(64))
    html_reference: Mapped[str | None] = mapped_column(Text)
    html_sha256: Mapped[str | None] = mapped_column(String(64))
    manifest_reference: Mapped[str | None] = mapped_column(Text)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    investigation: Mapped[Investigation] = relationship(back_populates="browser_runs")


class ApplicationSetting(Base):
    __tablename__ = "application_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class SearchTemplate(Base):
    __tablename__ = "search_templates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(100), default="Custom")
    query_template: Mapped[str] = mapped_column(Text)
    engines: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["google", "bing"])
    query_types: Mapped[list[str]] = mapped_column(
        JSON, default=lambda: ["username", "email", "domain"]
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    investigation_id: Mapped[str | None] = mapped_column(String(36), index=True)
    event_type: Mapped[str] = mapped_column(String(50))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

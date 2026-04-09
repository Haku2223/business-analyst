"""SQLAlchemy ORM models for all 7 database tables."""

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PipelineStatus(str, enum.Enum):
    unreviewed = "unreviewed"
    watch = "watch"
    deep_dive = "deep_dive"
    passed = "pass"


class Phase2Status(str, enum.Enum):
    not_started = "not_started"
    running = "running"
    complete = "complete"
    failed = "failed"


class JobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    paused = "paused"
    completed = "completed"
    failed = "failed"


class Company(Base):
    """Master record per unique org number."""

    __tablename__ = "companies"

    # Primary key
    orgnr: Mapped[str] = mapped_column(String(20), primary_key=True)

    # Core identity fields (from Allabolag export)
    bolagsnamn: Mapped[str | None] = mapped_column(String(255))
    bolagstyp: Mapped[str | None] = mapped_column(String(100))
    registreringsdatum: Mapped[str | None] = mapped_column(String(20))  # ISO date string
    ort: Mapped[str | None] = mapped_column(String(100))
    lan: Mapped[str | None] = mapped_column(String(100))
    hemsida: Mapped[str | None] = mapped_column(String(500))
    allabolag_url: Mapped[str | None] = mapped_column(String(500))
    ordforande: Mapped[str | None] = mapped_column(String(255))
    vd: Mapped[str | None] = mapped_column(String(255))

    # Financial data (stored in SEK öre = integer)
    omsattning: Mapped[int | None] = mapped_column(BigInteger)
    arets_resultat: Mapped[int | None] = mapped_column(BigInteger)
    aktiekapital: Mapped[int | None] = mapped_column(BigInteger)
    eget_kapital: Mapped[int | None] = mapped_column(BigInteger)
    summa_tillgangar: Mapped[int | None] = mapped_column(BigInteger)
    kassa_och_bank: Mapped[int | None] = mapped_column(BigInteger)
    loner_styrelse_vd: Mapped[int | None] = mapped_column(BigInteger)
    resultat_fore_skatt: Mapped[int | None] = mapped_column(BigInteger)
    rorelsresultat: Mapped[int | None] = mapped_column(BigInteger)
    resultat_efter_finansnetto: Mapped[int | None] = mapped_column(BigInteger)

    # Percentage/ratio fields
    vinstmarginal: Mapped[float | None] = mapped_column(Float)
    soliditet: Mapped[float | None] = mapped_column(Float)
    kassalikviditet: Mapped[float | None] = mapped_column(Float)
    skuldsattningsgrad: Mapped[float | None] = mapped_column(Float)

    # Other numeric
    antal_anstallda: Mapped[int | None] = mapped_column(Integer)

    # Date fields
    bokslutsperiod_slut: Mapped[str | None] = mapped_column(String(20))  # ISO date
    bokslutsperiod_start: Mapped[str | None] = mapped_column(String(20))

    # SNI codes (store as comma-separated strings for simplicity)
    sni_codes: Mapped[str | None] = mapped_column(Text)       # "33110,43210"
    sni_names: Mapped[str | None] = mapped_column(Text)       # "Reparation...,"

    # Additional columns from Allabolag export (stored for display)
    extra_data: Mapped[dict | None] = mapped_column(JSON)

    # Phase 2 enrichment
    historical_financials: Mapped[dict | None] = mapped_column(JSON)
    ai_description: Mapped[str | None] = mapped_column(Text)
    phase2_status: Mapped[str] = mapped_column(
        String(20), default="not_started"
    )
    phase2_error: Mapped[str | None] = mapped_column(Text)

    # Pipeline state
    pipeline_status: Mapped[str] = mapped_column(
        String(20), default="unreviewed"
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    batch_companies: Mapped[list["BatchCompany"]] = relationship(
        back_populates="company", lazy="select"
    )
    notes: Mapped[list["Note"]] = relationship(
        back_populates="company", lazy="select", order_by="Note.created_at.desc()"
    )
    pipeline_events: Mapped[list["PipelineEvent"]] = relationship(
        back_populates="company", lazy="select"
    )


class Batch(Base):
    """One row per file upload."""

    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(255))
    list_name: Mapped[str | None] = mapped_column(String(255))
    list_description: Mapped[str | None] = mapped_column(Text)
    upload_timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    filter_config_json: Mapped[dict | None] = mapped_column(JSON)
    row_count_uploaded: Mapped[int] = mapped_column(Integer, default=0)
    row_count_phase1: Mapped[int | None] = mapped_column(Integer)
    row_count_phase2a: Mapped[int | None] = mapped_column(Integer)
    row_count_phase2b: Mapped[int | None] = mapped_column(Integer)

    # Relationships
    batch_companies: Mapped[list["BatchCompany"]] = relationship(
        back_populates="batch", lazy="select"
    )
    phase2_jobs: Mapped[list["Phase2Job"]] = relationship(
        back_populates="batch", lazy="select"
    )


class BatchCompany(Base):
    """Join table linking companies to batches with Phase 1 result."""

    __tablename__ = "batch_companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    company_orgnr: Mapped[str] = mapped_column(
        ForeignKey("companies.orgnr"), index=True
    )
    phase1_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_filters: Mapped[list | None] = mapped_column(JSON)  # list of filter names

    # Relationships
    batch: Mapped["Batch"] = relationship(back_populates="batch_companies")
    company: Mapped["Company"] = relationship(back_populates="batch_companies")


class PipelineEvent(Base):
    """Append-only log of every pipeline status change."""

    __tablename__ = "pipeline_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_orgnr: Mapped[str] = mapped_column(
        ForeignKey("companies.orgnr"), index=True
    )
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    user_name: Mapped[str | None] = mapped_column(String(100))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    company: Mapped["Company"] = relationship(back_populates="pipeline_events")


class Note(Base):
    """Append-only notes attached to companies."""

    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_orgnr: Mapped[str] = mapped_column(
        ForeignKey("companies.orgnr"), index=True
    )
    note_text: Mapped[str] = mapped_column(Text)
    user_name: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    company: Mapped["Company"] = relationship(back_populates="notes")


class FilterPreset(Base):
    """Saved filter configurations."""

    __tablename__ = "filter_presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100))
    config_json: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Phase2Job(Base):
    """Background job tracking for Phase 2."""

    __tablename__ = "phase2_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    companies_total: Mapped[int] = mapped_column(Integer, default=0)
    companies_done: Mapped[int] = mapped_column(Integer, default=0)
    last_completed_orgnr: Mapped[str | None] = mapped_column(String(20))
    errors_json: Mapped[list | None] = mapped_column(JSON)

    batch: Mapped["Batch"] = relationship(back_populates="phase2_jobs")

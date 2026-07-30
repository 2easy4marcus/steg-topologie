"""Pydantic contracts for the source-provenance registry.

Every rule expressed here has a matching CHECK constraint (or guard trigger)
in ``migrations/0001_source_registry.sql``.  The two layers are deliberately
kept in exact parity: a value these contracts accept must be insertable, and
a value these contracts reject must be rejected by the database too.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

ID_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
CHECKSUM_PATTERN = r"^[a-f0-9]{64}$"


class PublicationClass(str, Enum):
    PUBLIC = "public"
    PRIVATE_RESEARCH = "private_research"


class GateOutcome(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    QUARANTINE = "quarantine"


def _blank_to_none(value: Any) -> Any:
    """Normalize an optional free-text field: blank/whitespace becomes None."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


class RegistryContract(BaseModel):
    """Base for source-registry contracts.

    Contracts are frozen (provenance records are never mutated in place) and
    closed (an unexpected key is a bug in the caller, not something to ignore).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def _timestamps_must_be_timezone_aware(
        cls, value: Any, info: ValidationInfo
    ) -> Any:
        if isinstance(value, datetime) and (
            value.tzinfo is None or value.utcoffset() is None
        ):
            raise ValueError(f"{info.field_name} must be timezone-aware")
        return value

    @field_validator("license_id", mode="before", check_fields=False)
    @classmethod
    def _normalize_license_id(cls, value: Any) -> Any:
        return _blank_to_none(value)


class DatasetSource(RegistryContract):
    source_id: str = Field(pattern=ID_PATTERN)
    title: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    source_url: str | None = None
    geographic_coverage: str | None = None
    temporal_coverage: str | None = None
    license_id: str | None = None
    publication_class: PublicationClass
    refresh_policy: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    acquisition_description: str = Field(min_length=1)

    @model_validator(mode="after")
    def public_requires_license(self):
        if (
            self.publication_class == PublicationClass.PUBLIC
            and not self.license_id
        ):
            raise ValueError("public source requires license_id")
        return self


class SourceArtifact(RegistryContract):
    artifact_id: str = Field(pattern=ID_PATTERN)
    source_id: str = Field(pattern=ID_PATTERN)
    relative_path: str = Field(min_length=1)
    checksum_sha256: str = Field(pattern=CHECKSUM_PATTERN)
    byte_size: int = Field(ge=0)
    retrieved_at: datetime
    registered_at: datetime | None = None
    media_type: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    license_id: str | None = None


class QuarantinedRecord(RegistryContract):
    quarantine_id: str = Field(pattern=ID_PATTERN)
    source_id: str = Field(pattern=ID_PATTERN)
    artifact_id: str | None = Field(default=None, pattern=ID_PATTERN)
    record_key: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    safe_detail: str = Field(min_length=1)
    quarantined_at: datetime

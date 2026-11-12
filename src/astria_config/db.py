"""Tabel config (SQLAlchemy Core): berjalan di PostgreSQL (server) dan SQLite (lokal dan uji).

Tahap ini memakai `metadata.create_all` seperti layanan lain; Alembic menyusul di tahap berikutnya (Tahap 0 note).
Outbox dan `processed_events` berasal dari `astria_common.outbox`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
_JSON = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime | None) -> datetime | None:
    """SQLite mengembalikan datetime tanpa zona; semua waktu config adalah UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def iso(value: datetime | None) -> str | None:
    value = aware(value)
    return value.isoformat().replace("+00:00", "Z") if value else None


setting = Table(
    "setting",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", _JSON, nullable=True),  # NULL untuk rahasia
    Column("is_secret", Boolean, nullable=False, default=False),
    Column("ciphertext", LargeBinary, nullable=True),
    Column("nonce", LargeBinary, nullable=True),
    Column("key_version", SmallInteger, nullable=True),
    Column("version", Integer, nullable=False),
    Column("updated_by", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, default=utcnow),
    CheckConstraint("is_secret = (ciphertext IS NOT NULL)", name="setting_secret_punya_ciphertext"),
)

# Hanya nilai biasa; rahasia tidak punya riwayat nilai (ciphertext lama tidak disimpan).
setting_history = Table(
    "setting_history",
    metadata,
    Column("key", Text, primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("value", _JSON, nullable=True),
    Column("changed_by", Text, nullable=False),
    Column("changed_at", DateTime(timezone=True), nullable=False, default=utcnow),
    Column("reason", Text, nullable=True),
)

catalog_version = Table(
    "catalog_version",
    metadata,
    Column("kind", Text, primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("status", Text, nullable=False),
    Column("content", _JSON, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("schema_version", Text, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, default=utcnow),
    Column("reviewed_by", Text, nullable=True),
    Column("activated_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("status IN ('draft','ditinjau','aktif','arsip')", name="catalog_status_valid"),
    # Empat mata ditegakkan juga di basis data: bug di kode tidak boleh melewatinya diam-diam.
    CheckConstraint("reviewed_by IS NULL OR reviewed_by <> created_by", name="catalog_empat_mata"),
)
Index(
    "catalog_satu_aktif",
    catalog_version.c.kind,
    unique=True,
    postgresql_where=catalog_version.c.status == "aktif",
    sqlite_where=catalog_version.c.status == "aktif",
)

channel_policy = Table(
    "channel_policy",
    metadata,
    Column("channel_id", Text, primary_key=True),
    Column("active", Boolean, nullable=False),
    Column("max_detail", Text, nullable=False),
    Column("min_band", Text, nullable=True),
    Column("version", Integer, nullable=False),
    Column("updated_by", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, default=utcnow),
    CheckConstraint("max_detail IN ('ringkas','sedang','penuh')", name="policy_max_detail_valid"),
    CheckConstraint("min_band IS NULL OR min_band IN ('RENDAH','SEDANG','TINGGI','KRITIS')", name="policy_band_valid"),
)


def row_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)

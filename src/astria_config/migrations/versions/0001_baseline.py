"""Baseline skema config: setting, riwayat setting, katalog, kebijakan kanal, outbox, dan processed_events.

Ditulis eksplisit (bukan dari metadata SQLAlchemy) supaya baseline tidak ikut berubah saat model berkembang.
CHECK empat mata dan satu-aktif-per-jenis ikut di sini; uji `test_migrations.py` membandingkannya dengan metadata.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _ts(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "setting",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", _JSON, nullable=True),
        sa.Column("is_secret", sa.Boolean(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("nonce", sa.LargeBinary(), nullable=True),
        sa.Column("key_version", sa.SmallInteger(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        _ts("updated_at"),
        sa.CheckConstraint("is_secret = (ciphertext IS NOT NULL)", name="setting_secret_punya_ciphertext"),
    )

    op.create_table(
        "setting_history",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("value", _JSON, nullable=True),
        sa.Column("changed_by", sa.Text(), nullable=False),
        _ts("changed_at"),
        sa.Column("reason", sa.Text(), nullable=True),
    )

    op.create_table(
        "catalog_version",
        sa.Column("kind", sa.Text(), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("content", _JSON, nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        _ts("created_at"),
        sa.Column("reviewed_by", sa.Text(), nullable=True),
        _ts("activated_at", nullable=True),
        sa.CheckConstraint("status IN ('draft','ditinjau','aktif','arsip')", name="catalog_status_valid"),
        sa.CheckConstraint("reviewed_by IS NULL OR reviewed_by <> created_by", name="catalog_empat_mata"),
    )
    op.create_index(
        "catalog_satu_aktif",
        "catalog_version",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("status = 'aktif'"),
        sqlite_where=sa.text("status = 'aktif'"),
    )

    op.create_table(
        "channel_policy",
        sa.Column("channel_id", sa.Text(), primary_key=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("max_detail", sa.Text(), nullable=False),
        sa.Column("min_band", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        _ts("updated_at"),
        sa.CheckConstraint("max_detail IN ('ringkas','sedang','penuh')", name="policy_max_detail_valid"),
        sa.CheckConstraint(
            "min_band IS NULL OR min_band IN ('RENDAH','SEDANG','TINGGI','KRITIS')", name="policy_band_valid"
        ),
    )

    # Outbox dan processed_events sama dengan `astria_common.outbox` (kolom JSONB langsung, seperti di common).
    op.create_table(
        "outbox",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("envelope", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("outbox_belum_terbit", "outbox", ["created_at"], postgresql_where=sa.text("published_at IS NULL"))

    op.create_table(
        "processed_events",
        sa.Column("consumer", sa.Text(), primary_key=True),
        sa.Column("event_id", sa.Text(), primary_key=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("processed_events")
    op.drop_index("outbox_belum_terbit", table_name="outbox")
    op.drop_table("outbox")
    op.drop_table("channel_policy")
    op.drop_index("catalog_satu_aktif", table_name="catalog_version")
    op.drop_table("catalog_version")
    op.drop_table("setting_history")
    op.drop_table("setting")

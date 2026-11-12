"""Kebijakan kanal Core dan pengaturan efektif minimal (subset identity#4, note app-config bagian 3.2 dan 4).

Hanya jalur baca `/svc/v1` dan seed dev. Rahasia tidak pernah disimpan di tabel `setting`: seed menolak kunci
yang tampak seperti rahasia. Endpoint admin, riwayat versi, katalog, dan `config.changed` belum ada.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

import orjson
from astria_common.component import Check
from astria_common.db import Database
from astria_common.log import get_logger
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

log = get_logger(__name__)

CHANNEL_ID = r"^[a-z][a-z0-9-]{0,62}$"
SCOPE = r"^[a-z][a-z0-9_]{0,39}$"
MaxDetail = Literal["minimal", "ringkas", "penuh"]
Band = Literal["RENDAH", "SEDANG", "TINGGI", "KRITIS"]
_SECRET_KEY = re.compile(r"secret|password|token|api_key|private", re.IGNORECASE)


def _sql_in(literal: Any) -> str:
    return "(" + ", ".join(repr(v) for v in get_args(literal)) + ")"


metadata = MetaData()

channel_policy = Table(
    "channel_policy",
    metadata,
    Column("channel_id", Text, primary_key=True),
    Column("active", Boolean, nullable=False),
    Column("max_detail", Text, nullable=False),
    Column("band_min", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(f"max_detail IN {_sql_in(MaxDetail)}", name="channel_policy_max_detail"),
    CheckConstraint(f"band_min IN {_sql_in(Band)}", name="channel_policy_band_min"),
)
setting = Table(
    "setting",
    metadata,
    Column("scope", Text, primary_key=True),
    Column("key", Text, primary_key=True),
    Column("value", JSON().with_variant(JSONB(), "postgresql"), nullable=False),
    Column("value_version", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


class ChannelPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(pattern=CHANNEL_ID)
    active: bool = False
    max_detail: MaxDetail = "minimal"
    band_min: Band = "TINGGI"


class Seed(BaseModel):
    """Berkas seed dev: `{"channel_policies": [...], "settings": {"<scope>": {"<scope>.<nama>": nilai}}}`."""

    model_config = ConfigDict(extra="forbid")

    channel_policies: list[ChannelPolicy] = Field(default_factory=list)
    settings: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> Seed:
        ids = [p.channel_id for p in self.channel_policies]
        if len(set(ids)) != len(ids):
            raise ValueError("channel_id ganda di berkas seed")
        for scope, values in self.settings.items():
            for key in values:
                check_setting_key(scope, key)
        return self


def check_setting_key(scope: str, key: str) -> None:
    """Aturan tabel `setting` untuk semua penulis: kunci berawalan lingkupnya dan bukan rahasia."""
    if not re.match(SCOPE, scope):
        raise ValueError(f"lingkup tidak valid: {scope!r}")
    if not key.startswith(f"{scope}."):
        raise ValueError(f"kunci {key!r} harus diawali '{scope}.'")
    if _SECRET_KEY.search(key):
        raise ValueError(f"kunci {key!r} tampak seperti rahasia; rahasia tidak disimpan di pengaturan")


def _canonical(value: Any) -> bytes:
    # Bandingkan sebagai JSON: di Python `False == 0`, padahal tipe nilai pengaturan ikut bermakna.
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)


def load_seed(path: Path) -> Seed:
    """Baca dan validasi berkas seed saat rakit aplikasi: berkas rusak menghentikan start."""
    return Seed.model_validate_json(path.read_bytes())


async def list_policies(conn: AsyncConnection) -> list[ChannelPolicy]:
    cols = [channel_policy.c[n] for n in ChannelPolicy.model_fields]
    rows = (await conn.execute(select(*cols).order_by(channel_policy.c.channel_id))).mappings()
    return [ChannelPolicy(**row) for row in rows]


async def effective(conn: AsyncConnection, scope: str) -> dict[str, Any]:
    rows = await conn.execute(select(setting.c.key, setting.c.value).where(setting.c.scope == scope))
    return dict(rows.all())


async def apply_seed(conn: AsyncConnection, seed: Seed) -> None:
    """Idempoten: kebijakan dan pengaturan seed dibuat atau disamakan; `value_version` naik hanya bila nilai berubah."""
    for policy in seed.channel_policies:
        values = policy.model_dump(exclude={"channel_id"})
        found = await conn.scalar(
            select(channel_policy.c.channel_id).where(channel_policy.c.channel_id == policy.channel_id)
        )
        if found is None:
            await conn.execute(insert(channel_policy).values(channel_id=policy.channel_id, **values))
        else:
            await conn.execute(
                update(channel_policy)
                .where(channel_policy.c.channel_id == policy.channel_id)
                .values(**values, updated_at=func.now())
            )
    for scope, values in seed.settings.items():
        for key, value in values.items():
            check_setting_key(scope, key)
            where = (setting.c.scope == scope) & (setting.c.key == key)
            row = (await conn.execute(select(setting.c.value, setting.c.value_version).where(where))).first()
            if row is None:
                await conn.execute(insert(setting).values(scope=scope, key=key, value=value, value_version=1))
            elif _canonical(row.value) != _canonical(value):
                await conn.execute(
                    update(setting)
                    .where(where)
                    .values(value=value, value_version=row.value_version + 1, updated_at=func.now())
                )


@dataclass
class Seeder:
    """Komponen: terapkan seed dev sekali setelah basis data siap. /ready `down` sampai selesai."""

    db: Database
    seed: Seed
    name: str = "seed"
    required: bool = True
    done: bool = False
    error: str | None = None
    _task: asyncio.Task[None] | None = field(default=None, repr=False)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="config-seed")

    async def _run(self) -> None:
        await self.db.wait_ready()
        try:
            async with self.db.engine.begin() as conn:
                await apply_seed(conn, self.seed)
        except Exception as exc:  # noqa: BLE001 - terlihat di /ready; seed tidak boleh menjatuhkan proses
            self.error = type(exc).__name__
            log.error("seed_gagal", error=self.error)
            return
        self.done = True
        log.info("seed_diterapkan", channels=len(self.seed.channel_policies), scopes=sorted(self.seed.settings))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def check(self) -> Check:
        if self.done:
            return Check("ok", f"channels={len(self.seed.channel_policies)}")
        return Check("down", f"seed belum diterapkan ({self.error or 'menunggu basis data'})")

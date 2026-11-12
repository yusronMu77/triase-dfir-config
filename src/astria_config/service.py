"""Logika config: pengaturan, rahasia, kebijakan kanal, dan katalog berversi.

Urutan semua aksi tulis yang berdampak (note audit bagian 5.2): validasi, catat audit SINKRON (fail-closed), lalu satu
transaksi basis data yang memuat perubahan dan baris outbox `config.changed`. Bila audit tidak tersedia, perubahan
ditolak 503 dan tidak ada yang tersimpan. Konkurensi optimistik memakai `UPDATE ... WHERE version = :v` yang atomik.
Nilai pengaturan dan rahasia tidak pernah masuk log, event, atau audit (hanya hash kanonik nilai biasa).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from astria_common import outbox
from astria_common.audit_client import AuditUnavailable, audit_event
from astria_common.db import Database
from astria_common.errors import AstriaError
from astria_common.events import new_event
from astria_common.log import get_logger
from astria_contracts.events.audit_record import AuditRecord
from sqlalchemy import func, null, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from astria_config import catalogs, registry
from astria_config.auth import Actor
from astria_config.db import (
    catalog_version,
    channel_policy,
    iso,
    setting,
    setting_history,
    utcnow,
)
from astria_config.gateway_client import GatewayRejected, GatewayUnavailable
from astria_config.layers import Layers
from astria_config.metrics import (
    AUDIT_FAILURES,
    CATALOG_ACTIVE,
    CHANGES,
    SECRET_READS,
    SECRET_UNREADABLE,
)
from astria_config.secrets import MasterKey, Sealed, SecretUnreadable

log = get_logger(__name__)

MODEL_KEY = "model.api_key"


class AuditSink(Protocol):
    async def record(self, record: AuditRecord, *, act: dict[str, Any] | None = None) -> Any: ...


class UnavailableAudit:
    """Dipakai bila kunci layanan tidak ada: semua aksi tulis yang wajib audit ditolak (fail-closed)."""

    async def record(self, record: AuditRecord, *, act: dict[str, Any] | None = None) -> Any:
        raise AuditUnavailable("klien audit belum dikonfigurasi")


class SecretForwarder(Protocol):
    async def put_secret(self, key: str, value: str) -> None: ...


def conflict(message: str = "versi tidak cocok dengan yang tersimpan") -> AstriaError:
    return AstriaError("VERSI_KONFLIK", message, 409)


def invalid(message: str) -> AstriaError:
    return AstriaError("VALIDASI_GAGAL", message, 422)


def _setting_dict(resolved: Any) -> dict[str, Any]:
    return {
        "key": resolved.key,
        "value": resolved.value,
        "source": resolved.source,
        "version": resolved.version,
        "updated_by": resolved.updated_by,
        "updated_at": iso(resolved.updated_at),
        "locked": resolved.locked,
    }


def _policy_dict(row: Mapping[Any, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "channel_id": row["channel_id"],
        "active": bool(row["active"]),
        "max_detail": row["max_detail"],
        "version": int(row["version"]),
        "updated_by": row["updated_by"],
        "updated_at": iso(row["updated_at"]),
    }
    if row["min_band"]:
        out["min_band"] = row["min_band"]
    return out


def _catalog_dict(row: Mapping[Any, Any]) -> dict[str, Any]:
    return {
        "kind": row["kind"],
        "version": int(row["version"]),
        "status": row["status"],
        "content_hash": row["content_hash"],
        "schema_version": row["schema_version"],
        "created_by": row["created_by"],
        "reviewed_by": row["reviewed_by"],
        "activated_at": iso(row["activated_at"]),
    }


class ConfigService:
    def __init__(
        self,
        *,
        db: Database,
        layers: Layers,
        master: MasterKey,
        audit: AuditSink,
        gateway: SecretForwarder | None,
    ) -> None:
        self._db = db
        self._layers = layers
        self._master = master
        self._audit = audit
        self._gateway = gateway

    @property
    def layers(self) -> Layers:
        return self._layers

    def engine(self) -> AsyncEngine:
        if not self._db.is_ready:
            raise AstriaError("PENYIMPANAN_TIDAK_TERSEDIA", "penyimpanan belum siap", 503)
        return self._db.engine

    # ---- audit dan event -------------------------------------------------------------------------------------

    async def _audit_sync(
        self, actor: Actor, action: str, target_type: str, target_id: str, payload: dict[str, Any]
    ) -> None:
        # Kunci idempoten unik per percobaan: audit mengunci occurred_at ke kunci; percobaan ulang butuh kunci baru.
        record = AuditRecord(
            idempotency_key=f"config-{action}-{target_id}-{uuid.uuid4().hex[:16]}"[:200],
            occurred_at=utcnow(),
            actor=actor.sub,
            actor_type="user",
            action=action,
            target_type=target_type,
            target_id=target_id,
            via="api",
            payload=payload,
        )
        try:
            await self._audit.record(record)
        except AstriaError:
            AUDIT_FAILURES.inc()
            log.warning("audit_sinkron_gagal", action=action, target=target_type)
            raise
        except Exception as exc:  # noqa: BLE001 - klien audit tak terduga: tetap fail-closed
            AUDIT_FAILURES.inc()
            log.warning("audit_sinkron_gagal", action=action, target=target_type, error=type(exc).__name__)
            raise AuditUnavailable(type(exc).__name__) from exc

    @staticmethod
    async def _audit_async(
        conn: AsyncConnection,
        *,
        actor: str,
        actor_type: Literal["user", "service", "system"],
        action: str,
        target_type: str,
        target_id: str,
        payload: dict[str, Any],
        via: Literal["ui", "api", "bot", "cli", "system"] = "api",
    ) -> None:
        record = AuditRecord(
            idempotency_key=f"config-{action}-{target_id}-{uuid.uuid4().hex[:16]}"[:200],
            occurred_at=utcnow(),
            actor=actor,
            actor_type=actor_type,
            action=action,
            target_type=target_type,
            target_id=target_id,
            via=via,
            payload=payload,
        )
        await outbox.enqueue(conn, audit_event(record))

    @staticmethod
    async def _emit_changed(conn: AsyncConnection, **data: Any) -> None:
        await outbox.enqueue(conn, new_event("config.changed", {k: v for k, v in data.items() if v is not None}))

    # ---- pengaturan ------------------------------------------------------------------------------------------

    @staticmethod
    def _known(key: str, *, secret: bool) -> registry.Key:
        spec = registry.get(key)
        if spec is None or spec.secret != secret:
            raise AstriaError("KUNCI_TIDAK_DIKENAL", "kunci tidak ada di registri", 404)
        return spec

    async def list_settings(self) -> dict[str, Any]:
        async with self.engine().connect() as conn:
            rows = {r["key"]: r for r in (await conn.execute(select(setting))).mappings().all()}
        items: list[dict[str, Any]] = []
        for spec in registry.KEYS:
            row = rows.get(spec.name)
            if spec.secret:
                items.append(
                    {
                        "key": spec.name,
                        "terisi": row is not None and bool(row["is_secret"]),
                        "version": int(row["version"]) if row else 0,
                        "updated_by": row["updated_by"] if row else None,
                        "updated_at": iso(row["updated_at"]) if row else None,
                    }
                )
            else:
                items.append(_setting_dict(self._layers.resolve(spec, row)))
        return {"items": items}

    async def put_setting(
        self, actor: Actor, key: str, value: Any, if_version: int, reason: str | None
    ) -> dict[str, Any]:
        spec = registry.get(key)
        if spec is None:
            raise AstriaError("KUNCI_TIDAK_DIKENAL", "kunci tidak ada di registri", 404)
        if spec.secret:
            raise invalid("kunci ini rahasia; tulis lewat /v1/secrets")
        if not spec.editable:
            raise invalid("kunci ini tidak dapat diubah dari layar")
        if self._layers.locked(key):
            raise AstriaError("DIKUNCI_ENV", "kunci dikunci oleh variabel lingkungan", 409)
        try:
            normalized = spec.validate(value)
        except ValueError as exc:
            raise invalid(f"{key}: {exc}") from None

        engine = self.engine()
        async with engine.connect() as conn:
            row = (await conn.execute(select(setting).where(setting.c.key == key))).mappings().first()
        current = int(row["version"]) if row else 0
        if current != if_version:
            raise conflict()
        before = self._layers.resolve(spec, row).value
        new_version = current + 1
        await self._audit_sync(
            actor,
            "config.setting.changed",
            "setting",
            key,
            {
                "key": key,
                "version": new_version,
                "before_hash": catalogs.value_hash(before),
                "after_hash": catalogs.value_hash(normalized),
            },
        )
        now = utcnow()
        try:
            async with engine.begin() as conn:
                values = {"value": normalized, "version": new_version, "updated_by": actor.sub, "updated_at": now}
                if row is None:
                    await conn.execute(setting.insert().values(key=key, is_secret=False, **values))
                else:
                    result = await conn.execute(
                        update(setting).where(setting.c.key == key, setting.c.version == current).values(**values)
                    )
                    if result.rowcount != 1:
                        raise conflict()
                await conn.execute(
                    setting_history.insert().values(
                        key=key,
                        version=new_version,
                        value=normalized,
                        changed_by=actor.sub,
                        changed_at=now,
                        reason=reason,
                    )
                )
                await self._emit_changed(conn, scope="setting", key=key, value_version=new_version)
        except IntegrityError:
            raise conflict() from None
        CHANGES.labels("setting").inc()
        log.info("pengaturan_diubah", key=key, version=new_version, actor=actor.sub)
        resolved = self._layers.resolve(
            spec, {"value": normalized, "version": new_version, "updated_by": actor.sub, "updated_at": now}
        )
        return _setting_dict(resolved)

    async def put_secret(self, actor: Actor, key: str, value: str) -> dict[str, Any]:
        self._known(key, secret=True)
        engine = self.engine()
        async with engine.connect() as conn:
            row = (await conn.execute(select(setting).where(setting.c.key == key))).mappings().first()
        current = int(row["version"]) if row else 0
        new_version = current + 1
        # Audit tanpa nilai dan tanpa hash nilai: hash rahasia memungkinkan tebakan luring.
        await self._audit_sync(
            actor,
            "config.secret.updated",
            "config_key",
            key,
            {"key": key, "changed": True, "value_recorded": False, "version": new_version},
        )
        sealed = self._master.seal(key, new_version, value)
        now = utcnow()
        try:
            async with engine.begin() as conn:
                values = {
                    "value": null(),
                    "is_secret": True,
                    "ciphertext": sealed.ciphertext,
                    "nonce": sealed.nonce,
                    "key_version": sealed.key_version,
                    "version": new_version,
                    "updated_by": actor.sub,
                    "updated_at": now,
                }
                if row is None:
                    await conn.execute(setting.insert().values(key=key, **values))
                else:
                    result = await conn.execute(
                        update(setting).where(setting.c.key == key, setting.c.version == current).values(**values)
                    )
                    if result.rowcount != 1:
                        raise conflict()
                await self._emit_changed(conn, scope="secret", key=key, value_version=new_version)
        except IntegrityError:
            raise conflict() from None
        CHANGES.labels("secret").inc()
        log.info("rahasia_diubah", key=key, version=new_version, actor=actor.sub)
        return {"key": key, "terisi": True, "version": new_version, "updated_by": actor.sub, "updated_at": iso(now)}

    async def forward_gateway_secret(self, actor: Actor, key: str, value: str) -> None:
        """Teruskan rahasia kanal ke gateway lewat memori; tidak disimpan di Core."""
        await self._audit_sync(
            actor,
            "config.secret.forwarded",
            "gateway_secret",
            key,
            {"key": key, "target": "gateway", "value_recorded": False},
        )
        if self._gateway is None:
            raise AstriaError("GATEWAY_TIDAK_TERSEDIA", "penerusan ke gateway tidak dikonfigurasi", 503)
        try:
            await self._gateway.put_secret(key, value)
        except GatewayRejected as exc:
            raise AstriaError(exc.code, exc.message, exc.status) from None
        except GatewayUnavailable as exc:
            raise AstriaError("GATEWAY_TIDAK_TERSEDIA", "gateway tidak dapat menerima rahasia", 503) from exc
        CHANGES.labels("gateway_secret").inc()
        log.info("rahasia_kanal_diteruskan", key=key, actor=actor.sub)

    async def effective_settings(self, scope: str) -> dict[str, Any]:
        specs = [k for k in registry.KEYS if k.scope == scope and not k.secret]
        if not specs:
            raise AstriaError("KUNCI_TIDAK_DIKENAL", "lingkup tidak dikenal", 404)
        async with self.engine().connect() as conn:
            rows = {
                r["key"]: r
                for r in (await conn.execute(select(setting).where(setting.c.key.in_([s.name for s in specs]))))
                .mappings()
                .all()
            }
        return {"scope": scope, "values": {s.name: self._layers.resolve(s, rows.get(s.name)).value for s in specs}}

    async def read_model_key(self, service: str) -> dict[str, str]:
        """Rahasia model untuk narrative. Pembacaan dicatat ke audit (asinkron, tanpa nilai)."""
        async with self.engine().begin() as conn:
            row = (await conn.execute(select(setting).where(setting.c.key == MODEL_KEY))).mappings().first()
            if row is None or not row["is_secret"]:
                SECRET_READS.labels("belum_diisi").inc()
                raise AstriaError("RAHASIA_BELUM_DIISI", "rahasia belum diisi", 404)
            sealed = Sealed(bytes(row["ciphertext"]), bytes(row["nonce"]), int(row["key_version"]))
            try:
                plaintext = self._master.open(MODEL_KEY, int(row["version"]), sealed)
            except SecretUnreadable:
                SECRET_UNREADABLE.inc()
                SECRET_READS.labels("tidak_terbaca").inc()
                log.error("rahasia_tidak_terbaca", key=MODEL_KEY)
                raise AstriaError(
                    "RAHASIA_TIDAK_TERBACA", "rahasia tidak dapat dibuka; tulis ulang nilainya", 503
                ) from None
            await self._audit_async(
                conn,
                actor=f"svc:{service}",
                actor_type="service",
                action="config.secret.read",
                target_type="config_key",
                target_id=MODEL_KEY,
                payload={"key": MODEL_KEY, "version": int(row["version"]), "value_recorded": False},
            )
        SECRET_READS.labels("ok").inc()
        return {"key": MODEL_KEY, "value": plaintext}

    # ---- kebijakan kanal -------------------------------------------------------------------------------------

    async def list_policies(self) -> dict[str, Any]:
        async with self.engine().connect() as conn:
            rows = (await conn.execute(select(channel_policy).order_by(channel_policy.c.channel_id))).mappings().all()
        return {"items": [_policy_dict(r) for r in rows]}

    async def put_policy(
        self,
        actor: Actor,
        channel_id: str,
        *,
        active: bool,
        max_detail: str,
        min_band: str | None,
        if_version: int,
    ) -> dict[str, Any]:
        engine = self.engine()
        async with engine.connect() as conn:
            row = (
                (await conn.execute(select(channel_policy).where(channel_policy.c.channel_id == channel_id)))
                .mappings()
                .first()
            )
        current = int(row["version"]) if row else 0
        if current != if_version:
            raise conflict()
        new_version = current + 1
        after = {"active": active, "max_detail": max_detail, "min_band": min_band}
        before = _policy_dict(row) if row else None
        await self._audit_sync(
            actor,
            "config.channel_policy.changed",
            "channel_policy",
            channel_id,
            {
                "channel_id": channel_id,
                "version": new_version,
                "before_hash": catalogs.value_hash({k: before.get(k) for k in after}) if before else None,
                "after_hash": catalogs.value_hash(after),
            },
        )
        now = utcnow()
        values = {**after, "version": new_version, "updated_by": actor.sub, "updated_at": now}
        try:
            async with engine.begin() as conn:
                if row is None:
                    await conn.execute(channel_policy.insert().values(channel_id=channel_id, **values))
                else:
                    result = await conn.execute(
                        update(channel_policy)
                        .where(channel_policy.c.channel_id == channel_id, channel_policy.c.version == current)
                        .values(**values)
                    )
                    if result.rowcount != 1:
                        raise conflict()
                await self._emit_changed(conn, scope="channel_policy", key=channel_id, value_version=new_version)
        except IntegrityError:
            raise conflict() from None
        CHANGES.labels("channel_policy").inc()
        log.info("kebijakan_kanal_diubah", channel=channel_id, version=new_version, actor=actor.sub)
        return _policy_dict({"channel_id": channel_id, "updated_at": now, "updated_by": actor.sub, **values})

    async def seed_policies(self, channel_ids: tuple[str, ...]) -> int:
        """Semai kebijakan awal (aktif, ringkas) untuk kanal yang belum punya; idempoten dan tanpa audit (sistem)."""
        created = 0
        async with self.engine().begin() as conn:
            for channel_id in channel_ids:
                exists = (
                    await conn.execute(
                        select(channel_policy.c.channel_id).where(channel_policy.c.channel_id == channel_id)
                    )
                ).first()
                if exists:
                    continue
                await conn.execute(
                    channel_policy.insert().values(
                        channel_id=channel_id,
                        active=True,
                        max_detail="ringkas",
                        min_band=None,
                        version=1,
                        updated_by="sistem",
                        updated_at=utcnow(),
                    )
                )
                created += 1
        return created

    # ---- katalog ---------------------------------------------------------------------------------------------

    @staticmethod
    def _kind(kind: str) -> str:
        if kind not in catalogs.KINDS:
            raise AstriaError("KATALOG_TIDAK_DITEMUKAN", "jenis katalog tidak dikenal", 404)
        return kind

    async def _version_row(self, conn: AsyncConnection, kind: str, version: int) -> Mapping[Any, Any]:
        row = (
            (
                await conn.execute(
                    select(catalog_version).where(catalog_version.c.kind == kind, catalog_version.c.version == version)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AstriaError("KATALOG_TIDAK_DITEMUKAN", "versi katalog tidak ditemukan", 404)
        return row

    async def list_versions(self, kind: str) -> dict[str, Any]:
        self._kind(kind)
        async with self.engine().connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(catalog_version)
                        .where(catalog_version.c.kind == kind)
                        .order_by(catalog_version.c.version)
                    )
                )
                .mappings()
                .all()
            )
        return {"items": [_catalog_dict(r) for r in rows]}

    async def create_draft(
        self, actor: Actor, kind: str, content: dict[str, Any], schema_version: str | None
    ) -> dict[str, Any]:
        self._kind(kind)
        errors = catalogs.validate_document(kind, content)
        if errors:
            raise invalid("katalog tidak valid: " + "; ".join(errors[:5]))
        digest = catalogs.content_hash(content)
        now = utcnow()
        try:
            async with self.engine().begin() as conn:
                latest = (
                    await conn.execute(
                        select(func.max(catalog_version.c.version)).where(catalog_version.c.kind == kind)
                    )
                ).scalar()
                number = int(latest or 0) + 1
                await conn.execute(
                    catalog_version.insert().values(
                        kind=kind,
                        version=number,
                        status="draft",
                        content=content,
                        content_hash=digest,
                        schema_version=schema_version or catalogs.SCHEMA_VERSION,
                        created_by=actor.sub,
                        created_at=now,
                    )
                )
                await self._audit_async(
                    conn,
                    actor=actor.sub,
                    actor_type="user",
                    action="config.catalog.drafted",
                    target_type="catalog",
                    target_id=f"{kind}:{number}",
                    payload={"kind": kind, "version": number, "content_hash": digest},
                )
        except IntegrityError:
            raise conflict("versi katalog bentrok; ulangi") from None
        return {
            "kind": kind,
            "version": number,
            "status": "draft",
            "content_hash": digest,
            "schema_version": schema_version or catalogs.SCHEMA_VERSION,
            "created_by": actor.sub,
            "reviewed_by": None,
            "activated_at": None,
        }

    async def review(self, actor: Actor, kind: str, version: int) -> dict[str, Any]:
        self._kind(kind)
        try:
            async with self.engine().begin() as conn:
                row = await self._version_row(conn, kind, version)
                if row["status"] != "draft":
                    raise AstriaError("STATUS_TIDAK_VALID", "hanya draf yang dapat ditinjau", 409)
                if row["created_by"] == actor.sub:
                    raise AstriaError(
                        "PENINJAU_SAMA_DENGAN_PENULIS", "penulis tidak boleh meninjau versinya sendiri", 403
                    )
                result = await conn.execute(
                    update(catalog_version)
                    .where(
                        catalog_version.c.kind == kind,
                        catalog_version.c.version == version,
                        catalog_version.c.status == "draft",
                    )
                    .values(status="ditinjau", reviewed_by=actor.sub)
                )
                if result.rowcount != 1:
                    raise AstriaError("STATUS_TIDAK_VALID", "status versi berubah; ulangi", 409)
                await self._audit_async(
                    conn,
                    actor=actor.sub,
                    actor_type="user",
                    action="config.catalog.reviewed",
                    target_type="catalog",
                    target_id=f"{kind}:{version}",
                    payload={"kind": kind, "version": version, "content_hash": row["content_hash"]},
                )
        except IntegrityError:
            raise AstriaError(
                "PENINJAU_SAMA_DENGAN_PENULIS", "penulis tidak boleh meninjau versinya sendiri", 403
            ) from None
        return _catalog_dict({**row, "status": "ditinjau", "reviewed_by": actor.sub})

    async def activate(self, actor: Actor, kind: str, version: int) -> dict[str, Any]:
        self._kind(kind)
        engine = self.engine()
        async with engine.connect() as conn:
            row = await self._version_row(conn, kind, version)
            current = (
                (
                    await conn.execute(
                        select(catalog_version).where(
                            catalog_version.c.kind == kind, catalog_version.c.status == "aktif"
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row["status"] != "ditinjau":
            raise AstriaError("KATALOG_BELUM_DITINJAU", "versi harus berstatus ditinjau untuk diaktifkan", 409)
        if row["created_by"] == actor.sub:
            raise AstriaError("PENINJAU_SAMA_DENGAN_PENULIS", "penulis tidak boleh mengaktifkan versinya sendiri", 403)
        errors = catalogs.validate_document(kind, row["content"])
        if errors:
            raise invalid("katalog tidak valid: " + "; ".join(errors[:5]))
        await self._audit_sync(
            actor,
            "config.catalog.activated",
            "catalog",
            f"{kind}:{version}",
            {
                "kind": kind,
                "version": version,
                "before_hash": current["content_hash"] if current else None,
                "after_hash": row["content_hash"],
            },
        )
        now = utcnow()
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    update(catalog_version)
                    .where(catalog_version.c.kind == kind, catalog_version.c.status == "aktif")
                    .values(status="arsip")
                )
                result = await conn.execute(
                    update(catalog_version)
                    .where(
                        catalog_version.c.kind == kind,
                        catalog_version.c.version == version,
                        catalog_version.c.status == "ditinjau",
                    )
                    .values(status="aktif", activated_at=now)
                )
                if result.rowcount != 1:
                    raise AstriaError("KATALOG_BELUM_DITINJAU", "status versi berubah; ulangi", 409)
                await self._emit_changed(
                    conn, scope="catalog", kind=kind, catalog_version=version, content_hash=row["content_hash"]
                )
        except IntegrityError:
            raise conflict("ada aktivasi lain yang bersamaan; ulangi") from None
        CHANGES.labels("catalog").inc()
        CATALOG_ACTIVE.labels(kind).set(version)
        log.info("katalog_diaktifkan", kind=kind, version=version, actor=actor.sub)
        return _catalog_dict({**row, "status": "aktif", "activated_at": now})

    async def _active_row(self, conn: AsyncConnection, kind: str) -> Mapping[Any, Any]:
        row = (
            (
                await conn.execute(
                    select(catalog_version).where(catalog_version.c.kind == kind, catalog_version.c.status == "aktif")
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AstriaError("KATALOG_TIDAK_DITEMUKAN", "belum ada versi aktif untuk katalog ini", 404)
        return row

    async def active_catalog(self, kind: str) -> dict[str, Any]:
        self._kind(kind)
        async with self.engine().connect() as conn:
            row = await self._active_row(conn, kind)
        return {**row["content"], "catalog_version": int(row["version"]), "content_hash": row["content_hash"]}

    async def coverage(self, kind: str) -> dict[str, Any]:
        self._kind(kind)
        async with self.engine().connect() as conn:
            row = await self._active_row(conn, kind)
            langkah = None
            if kind == "pola_insiden":
                langkah_row = (
                    (
                        await conn.execute(
                            select(catalog_version).where(
                                catalog_version.c.kind == "langkah", catalog_version.c.status == "aktif"
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                langkah = langkah_row["content"] if langkah_row else None
        issues = catalogs.lint_references(kind, row["content"], langkah)
        return {"kind": kind, "catalog_version": int(row["version"]), "complete": not issues, "issues": issues}

    async def refresh_catalog_gauge(self) -> None:
        async with self.engine().connect() as conn:
            rows = (
                await conn.execute(
                    select(catalog_version.c.kind, catalog_version.c.version).where(catalog_version.c.status == "aktif")
                )
            ).all()
        active = {r[0]: int(r[1]) for r in rows}
        for kind in catalogs.KINDS:
            CATALOG_ACTIVE.labels(kind).set(active.get(kind, 0))

    async def catalog_seed(self, kind: str, content: dict[str, Any], author: str) -> int:
        """Khusus bootstrap dev: versi berikutnya langsung aktif, tanpa tinjauan. Ditolak di prod oleh CLI."""
        self._kind(kind)
        errors = catalogs.validate_document(kind, content)
        if errors:
            raise invalid("katalog tidak valid: " + "; ".join(errors[:5]))
        digest = catalogs.content_hash(content)
        now = utcnow()
        async with self.engine().begin() as conn:
            latest = (
                await conn.execute(select(func.max(catalog_version.c.version)).where(catalog_version.c.kind == kind))
            ).scalar()
            number = int(latest or 0) + 1
            await conn.execute(
                update(catalog_version)
                .where(catalog_version.c.kind == kind, catalog_version.c.status == "aktif")
                .values(status="arsip")
            )
            await conn.execute(
                catalog_version.insert().values(
                    kind=kind,
                    version=number,
                    status="aktif",
                    content=content,
                    content_hash=digest,
                    schema_version=catalogs.SCHEMA_VERSION,
                    created_by=author,
                    created_at=now,
                    activated_at=now,
                )
            )
            await self._emit_changed(conn, scope="catalog", kind=kind, catalog_version=number, content_hash=digest)
        CATALOG_ACTIVE.labels(kind).set(number)
        return number

"""CLI config: `config validate`, `config catalog lint|hash|seed`, `config rotate-master`.

`validate`, `catalog lint`, dan `catalog hash` tidak butuh basis data (dipakai di CI). `catalog seed` dan
`rotate-master` memakai `ASTRIA_DATABASE_URL` layanan. Keluaran tidak pernah memuat nilai rahasia.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from astria_common import outbox
from astria_common.audit_client import AuditUnavailable
from astria_common.db import Database
from astria_common.errors import AstriaError

from astria_config import catalogs
from astria_config.db import metadata
from astria_config.layers import ENV_PREFIX, ConfigError, Layers, load_file_values
from astria_config.rotation import rotate_master
from astria_config.secrets import MasterKey, MasterKeyError
from astria_config.service import ConfigService, UnavailableAudit
from astria_config.settings import Settings


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"berkas tidak terbaca atau bukan JSON: {path} ({type(exc).__name__})") from None


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        values = load_file_values(Path(args.file))
        Layers(values, {k: v for k, v in os.environ.items() if k.startswith(ENV_PREFIX)})
    except ConfigError as exc:
        print(f"TIDAK VALID: {exc}", file=sys.stderr)
        return 1
    print(f"valid: {len(values)} kunci dari berkas")
    return 0


def cmd_catalog_lint(args: argparse.Namespace) -> int:
    errors = catalogs.validate_document(args.kind, _load_json(Path(args.file)))
    for e in errors:
        print(f"galat: {e}", file=sys.stderr)
    if not errors:
        print(f"valid: {args.kind}")
    return 1 if errors else 0


def cmd_catalog_hash(args: argparse.Namespace) -> int:
    print(catalogs.content_hash(_load_json(Path(args.file))))
    return 0


async def _with_service(settings: Settings, work):  # noqa: ANN001, ANN202 - pembantu kecil CLI
    if settings.database_url is None:
        raise SystemExit("ASTRIA_DATABASE_URL wajib diisi")
    db = Database(settings.database_url, schema=settings.db_schema, metadata=[outbox.metadata, metadata])
    await db.start()
    try:
        await db.wait_ready(30)
        return await work(db)
    finally:
        await db.stop()


def cmd_catalog_seed(args: argparse.Namespace) -> int:
    settings = Settings()
    if settings.env == "prod":
        print("ditolak: catalog seed hanya untuk dev, demo, dan test (prod wajib lewat tinjauan)", file=sys.stderr)
        return 2
    content = _load_json(Path(args.file))
    if not isinstance(content, dict):
        print("galat: dokumen harus objek", file=sys.stderr)
        return 1

    async def work(db: Database) -> int:
        service = ConfigService(
            db=db,
            layers=Layers(),
            master=MasterKey.generate(),  # tidak dipakai oleh seed
            audit=UnavailableAudit(),
            gateway=None,
        )
        return await service.catalog_seed(args.kind, content, args.by)

    try:
        number = asyncio.run(_with_service(settings, work))
    except (AstriaError, AuditUnavailable) as exc:
        print(f"galat: {exc.message}", file=sys.stderr)
        return 1
    print(f"katalog {args.kind} versi {number} aktif")
    return 0


def cmd_rotate_master(args: argparse.Namespace) -> int:
    settings = Settings()
    new_path = Path(args.new_key_file)
    if new_path.resolve() == settings.master_key_file.resolve():
        print("ditolak: berkas kunci baru sama dengan kunci induk saat ini", file=sys.stderr)
        return 2
    try:
        old = MasterKey.load(settings.master_key_file)
        new = MasterKey.load_or_create(new_path, create=True)
    except MasterKeyError as exc:
        print(f"galat: {exc}", file=sys.stderr)
        return 1

    async def work(db: Database) -> int:
        async with db.engine.begin() as conn:
            return await rotate_master(conn, old, new)

    count = asyncio.run(_with_service(settings, work))
    print(f"{count} rahasia dienkripsi ulang. Ganti ASTRIA_MASTER_KEY_FILE ke {new_path} lalu mulai ulang layanan;")
    print("simpan kunci lama sampai verifikasi selesai, lalu hapus dari mesin.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="config", description="CLI Astria config")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="validasi berkas config.yaml (dan env ASTRIA_CFG_*)")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_validate)

    catalog = sub.add_parser("catalog", help="alat katalog").add_subparsers(dest="catalog_command", required=True)
    p = catalog.add_parser("lint", help="validasi dokumen katalog terhadap skema jenisnya")
    p.add_argument("kind", choices=catalogs.KINDS)
    p.add_argument("file")
    p.set_defaults(func=cmd_catalog_lint)
    p = catalog.add_parser("hash", help="hitung content_hash kanonik")
    p.add_argument("file")
    p.set_defaults(func=cmd_catalog_hash)
    p = catalog.add_parser("seed", help="bootstrap dev: versi berikutnya langsung aktif (ditolak di prod)")
    p.add_argument("kind", choices=catalogs.KINDS)
    p.add_argument("file")
    p.add_argument("--by", required=True, help="nama pembuat yang dicatat")
    p.set_defaults(func=cmd_catalog_seed)

    p = sub.add_parser("rotate-master", help="enkripsi ulang semua rahasia dengan kunci induk baru")
    p.add_argument("--new-key-file", required=True)
    p.set_defaults(func=cmd_rotate_master)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()

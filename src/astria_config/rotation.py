"""Rotasi kunci induk: enkripsi ulang semua rahasia dalam satu transaksi (gagal satu = batal semua)."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from astria_config.db import setting
from astria_config.secrets import MasterKey, Sealed


async def rotate_master(conn: AsyncConnection, old: MasterKey, new: MasterKey) -> int:
    """Buka tiap rahasia dengan kunci lama, segel dengan kunci baru; `key_version` naik, nilai dan versinya tetap.

    Dipanggil di dalam transaksi pemanggil (`engine.begin()`): galat di tengah jalan membatalkan seluruhnya.
    """
    rows = (await conn.execute(select(setting).where(setting.c.is_secret.is_(True)))).mappings().all()
    for row in rows:
        plaintext = old.open(
            row["key"],
            int(row["version"]),
            Sealed(bytes(row["ciphertext"]), bytes(row["nonce"]), int(row["key_version"])),
        )
        sealed = new.seal(row["key"], int(row["version"]), plaintext, key_version=int(row["key_version"]) + 1)
        result = await conn.execute(
            update(setting)
            .where(setting.c.key == row["key"], setting.c.version == row["version"])
            .values(ciphertext=sealed.ciphertext, nonce=sealed.nonce, key_version=sealed.key_version)
        )
        if result.rowcount != 1:
            raise RuntimeError(f"rahasia berubah saat rotasi: {row['key']}")
    return len(rows)

"""Rahasia Core: AES-256-GCM dengan kunci data turunan HKDF-SHA256 dari kunci induk (note app-config bagian 5.4).

- Kunci induk: 32 byte acak, disimpan base64 di berkas 0600 (dibuat otomatis di dev dan test; `prod` wajib sudah ada).
- Kunci data: HKDF-SHA256(kunci induk, salt `astria-config`, info = versi kunci + nama kunci), jadi ciphertext
  satu kunci tidak bisa dipindah ke kunci lain.
- AAD = `nama|versi_nilai`: ciphertext terikat ke kunci dan versinya.
- Nilai tidak pernah dicatat; galat tidak memuat nilai.
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SALT = b"astria-config"
NONCE_BYTES = 12


class SecretUnreadable(Exception):
    """Ciphertext gagal diautentikasi (kunci induk salah, data diubah, atau terikat ke kunci/versi lain)."""


class MasterKeyError(ValueError):
    pass


@dataclass(frozen=True)
class Sealed:
    ciphertext: bytes
    nonce: bytes
    key_version: int


class MasterKey:
    def __init__(self, raw: bytes) -> None:
        if len(raw) != 32:
            raise MasterKeyError("kunci induk harus 32 byte")
        self._raw = raw

    def __repr__(self) -> str:
        return "MasterKey(<tersembunyi>)"

    @classmethod
    def generate(cls) -> MasterKey:
        return cls(secrets.token_bytes(32))

    @classmethod
    def load(cls, path: Path) -> MasterKey:
        try:
            raw = base64.b64decode(path.read_text(encoding="ascii").strip(), validate=True)
        except (OSError, UnicodeDecodeError, binascii.Error) as exc:
            raise MasterKeyError(f"kunci induk tidak terbaca ({type(exc).__name__})") from None
        return cls(raw)

    @classmethod
    def load_or_create(cls, path: Path, *, create: bool) -> MasterKey:
        if path.exists():
            return cls.load(path)
        if not create:
            raise MasterKeyError("berkas kunci induk tidak ada")
        key = cls.generate()
        try:
            key.save(path)
        except FileExistsError:  # replika lain menulis lebih dulu: pakai kunci yang menang
            return cls.load(path)
        except OSError as exc:
            raise MasterKeyError(
                f"kunci induk tidak dapat dibuat di {path} ({type(exc).__name__}); pasang volume yang dapat ditulis"
            ) from None
        return key

    def save(self, path: Path) -> None:
        """Tulis baru dengan izin 0600; menolak menimpa berkas yang sudah ada."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(base64.b64encode(self._raw))

    def _data_key(self, name: str, key_version: int) -> bytes:
        info = f"secrets-v{key_version}|{name}".encode()
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=SALT, info=info).derive(self._raw)

    def seal(self, name: str, value_version: int, plaintext: str, *, key_version: int = 1) -> Sealed:
        nonce = secrets.token_bytes(NONCE_BYTES)
        aad = f"{name}|{value_version}".encode()
        ciphertext = AESGCM(self._data_key(name, key_version)).encrypt(nonce, plaintext.encode(), aad)
        return Sealed(ciphertext, nonce, key_version)

    def open(self, name: str, value_version: int, sealed: Sealed) -> str:
        aad = f"{name}|{value_version}".encode()
        try:
            raw = AESGCM(self._data_key(name, sealed.key_version)).decrypt(sealed.nonce, sealed.ciphertext, aad)
        except InvalidTag:
            raise SecretUnreadable from None
        return raw.decode()

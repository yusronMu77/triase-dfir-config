"""Lapisan nilai pengaturan: bawaan < berkas < database < env terkunci (note app-config bagian 5.4).

Berkas dan env dibaca sekali saat start dan divalidasi dengan registri; nilai tidak valid, kunci tak dikenal, atau
rahasia di berkas membuat start ditolak (`ConfigError`). Nilai database dibaca per permintaan.

Env: `ASTRIA_CFG_<KUNCI>` dengan titik pada kunci diganti `__` dan huruf besar, mis. `chat.allow_t2` menjadi
`ASTRIA_CFG_CHAT__ALLOW_T2`. Nilai di-parse sesuai tipe (bool: true/false/1/0; angka; list dan objek: JSON).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from astria_config import registry
from astria_config.registry import Key

ENV_PREFIX = "ASTRIA_CFG_"
SOURCES = ("bawaan", "berkas", "database", "env")


class ConfigError(ValueError):
    """Konfigurasi tidak valid saat start. Pesan menyebut kunci, tidak pernah nilainya."""


@dataclass(frozen=True)
class Resolved:
    key: str
    value: Any
    source: str
    version: int
    locked: bool
    updated_by: str | None = None
    updated_at: datetime | None = None


def env_name(key: str) -> str:
    return ENV_PREFIX + key.replace(".", "__").upper()


def _parse_env(spec: Key, raw: str) -> Any:
    text = raw.strip()
    try:
        if spec.kind == "bool":
            lowered = text.lower()
            if lowered in {"true", "1"}:
                return True
            if lowered in {"false", "0"}:
                return False
            raise ValueError("harus true atau false")
        if spec.kind in {"int", "float"}:
            return spec.validate(int(text) if spec.kind == "int" else float(text))
        if spec.kind in {"list", "object"}:
            return spec.validate(json.loads(text))
        if spec.nullable and text == "":
            return None
        return spec.validate(text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{env_name(spec.name)}: nilai tidak valid ({_reason(exc)})") from None


def _reason(exc: Exception) -> str:
    return "bukan JSON" if isinstance(exc, json.JSONDecodeError) else str(exc)


def load_file_values(path: Path) -> dict[str, Any]:
    """Baca berkas YAML bertingkat `{lingkup: {nama: nilai}}` menjadi kunci bertitik yang sudah divalidasi."""
    import yaml  # hanya bila berkas dipakai: image dev lama belum membawa pyyaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"berkas konfigurasi tidak terbaca: {type(exc).__name__}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("berkas konfigurasi harus berupa pemetaan")
    values: dict[str, Any] = {}
    for scope, entries in data.items():
        if not isinstance(entries, dict):
            raise ConfigError(f"berkas: {scope!s} harus berupa pemetaan nama -> nilai")
        for name, value in entries.items():
            key = f"{scope}.{name}"
            spec = registry.get(key)
            if spec is None:
                raise ConfigError(f"berkas: kunci tidak dikenal: {key}")
            if spec.secret:
                raise ConfigError(f"berkas: rahasia tidak boleh ada di berkas: {key}")
            try:
                values[key] = spec.validate(value)
            except ValueError as exc:
                raise ConfigError(f"berkas: {key}: {exc}") from None
    return values


class Layers:
    def __init__(self, file_values: Mapping[str, Any] | None = None, env: Mapping[str, str] | None = None) -> None:
        self._file = dict(file_values or {})
        self._env: dict[str, Any] = {}
        environ = env or {}
        for spec in registry.KEYS:
            raw = environ.get(env_name(spec.name))
            if raw is None:
                continue
            if spec.secret:
                raise ConfigError(f"{env_name(spec.name)}: rahasia tidak boleh diisi lewat env")
            self._env[spec.name] = _parse_env(spec, raw)
        known = {env_name(k.name) for k in registry.KEYS}
        stray = sorted(k for k in environ if k.startswith(ENV_PREFIX) and k not in known)
        if stray:
            raise ConfigError("variabel env tidak dikenal: " + ", ".join(stray))

    @classmethod
    def from_settings(cls, config_file: Path | None, env: Mapping[str, str]) -> Layers:
        return cls(load_file_values(config_file) if config_file else None, env)

    def locked(self, key: str) -> bool:
        return key in self._env

    def resolve(self, spec: Key, row: Mapping[Any, Any] | None = None) -> Resolved:
        """Nilai efektif satu kunci biasa. `row` = baris tabel `setting` (bila ada)."""
        key = spec.name
        if key in self._env:
            return Resolved(key, self._env[key], "env", int(row["version"]) if row else 0, True)
        if row is not None:
            return Resolved(
                key, row["value"], "database", int(row["version"]), False, row["updated_by"], row["updated_at"]
            )
        if key in self._file:
            return Resolved(key, self._file[key], "berkas", 0, False)
        return Resolved(key, spec.default, "bawaan", 0, False)

"""Registri kunci pengaturan Core: tipe, batas, bawaan, dan lingkup (note app-config bagian 5.5).

Registri hanya memuat kunci yang boleh ada; nilai bawaan tidak pernah berupa rahasia. Lingkup (`scope`) adalah bagian
kunci sebelum titik pertama. `validate` mengembalikan nilai ternormalisasi atau melempar `ValueError` berpesan aman
(tidak memuat nilai).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
_CRON = re.compile(r"^\S+( \S+){4}$")
_CRON_FIELD = re.compile(r"^[0-9*/,\-]+$")
_JAM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_HTTPS = re.compile(r"^https://[^\s/?#]+(/[^\s]*)?$")
MODEL_BACKENDS = ("replay", "online", "lokal", "templat")


@dataclass(frozen=True)
class Key:
    name: str
    kind: str  # bool | int | float | str | enum | list | object | secret
    default: Any = None
    description: str = ""
    editable: bool = True
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    nullable: bool = False
    check: Callable[[Any], Any] | None = field(default=None, repr=False)

    @property
    def scope(self) -> str:
        return self.name.split(".", 1)[0]

    @property
    def secret(self) -> bool:
        return self.kind == "secret"

    def validate(self, value: Any) -> Any:
        """Nilai ternormalisasi, atau `ValueError` (pesan tanpa nilai)."""
        if value is None:
            if self.nullable:
                return None
            raise ValueError("nilai tidak boleh kosong")
        if self.kind == "secret":
            raise ValueError("kunci rahasia tidak punya nilai biasa")
        if self.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError("harus true atau false")
            return value
        if self.kind in {"int", "float"}:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError("harus berupa angka")
            if self.kind == "int":
                if isinstance(value, float):
                    if not value.is_integer():
                        raise ValueError("harus bilangan bulat")
                    value = int(value)
            else:
                value = float(value)
            self._range(value)
            return value
        if self.kind == "str":
            if not isinstance(value, str):
                raise ValueError("harus berupa teks")
            if len(value) > 2048 or "\x00" in value:
                raise ValueError("teks tidak valid")
            return self.check(value) if self.check else value
        if self.kind == "enum":
            if value not in self.choices:
                raise ValueError("harus salah satu dari: " + ", ".join(self.choices))
            return value
        if self.kind == "list":
            if not isinstance(value, list):
                raise ValueError("harus berupa daftar")
            if len(value) > 200:
                raise ValueError("daftar terlalu panjang")
            return self.check(value) if self.check else value
        if self.kind == "object":
            if not isinstance(value, dict):
                raise ValueError("harus berupa objek")
            return self.check(value) if self.check else value
        raise ValueError("tipe kunci tidak dikenal")  # pragma: no cover - dijaga uji registri

    def _range(self, value: float) -> None:
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"minimal {self.minimum:g}")
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f"maksimal {self.maximum:g}")


def _chain(value: list[Any]) -> list[str]:
    if not value:
        raise ValueError("rantai backend tidak boleh kosong")
    if any(v not in MODEL_BACKENDS for v in value):
        raise ValueError("backend harus salah satu dari: " + ", ".join(MODEL_BACKENDS))
    if len(set(value)) != len(value):
        raise ValueError("backend tidak boleh berulang")
    return list(value)


def _hosts(value: list[Any]) -> list[str]:
    if not all(isinstance(v, str) and _HOST.match(v) for v in value):
        raise ValueError("setiap host harus nama host huruf kecil yang valid")
    return list(value)


def _roots(value: list[Any]) -> list[str]:
    if not all(isinstance(v, str) and v and len(v) <= 512 and "\x00" not in v for v in value):
        raise ValueError("setiap jalur harus teks tidak kosong")
    return list(value)


def _cron(value: str) -> str:
    if not _CRON.match(value) or not all(_CRON_FIELD.match(part) for part in value.split(" ")):
        raise ValueError("harus ekspresi cron 5 kolom")
    return value


def _shifts(value: list[Any]) -> list[dict[str, str]]:
    out = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"nama", "jam"}
            or not isinstance(item["nama"], str)
            or not 0 < len(item["nama"]) <= 64
            or not isinstance(item["jam"], str)
            or not _JAM.match(item["jam"])
        ):
            raise ValueError("setiap shift berbentuk {nama, jam: HH:MM}")
        out.append({"nama": item["nama"], "jam": item["jam"]})
    return out


def _band(value: dict[str, Any]) -> dict[str, int]:
    if set(value) != {"kritis", "tinggi", "sedang"}:
        raise ValueError("harus berisi kritis, tinggi, dan sedang")
    nums = {}
    for name, v in value.items():
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 100:
            raise ValueError("ambang berupa bilangan bulat 1 sampai 100")
        nums[name] = v
    if not nums["kritis"] > nums["tinggi"] > nums["sedang"]:
        raise ValueError("ambang harus menurun: kritis > tinggi > sedang")
    return nums


def _public_url(value: str) -> str:
    if value and not _HTTPS.match(value):
        raise ValueError("harus URL https atau kosong")
    return value


KEYS: tuple[Key, ...] = (
    Key(
        "model.chain",
        "list",
        list(MODEL_BACKENDS),
        "Urutan backend narasi; gagal atau lewat batas, jalur templat",
        check=_chain,
    ),
    Key("model.allowlist_hosts", "list", [], "Egress model hanya ke host ini", check=_hosts),
    Key("model.api_key", "secret", None, "Kunci API model (tulis-saja, dibaca narrative)"),
    Key("model.timeout_seconds", "int", 20, "Batas waktu panggilan model (detik)", minimum=1, maximum=120),
    Key("score.band", "object", {"kritis": 70, "tinggi": 50, "sedang": 30}, "Ambang band skor", check=_band),
    Key("ingest.allowed_roots", "list", [], "Sumber dan jalur yang diizinkan untuk impor", check=_roots),
    Key("schedule.digest", "str", "0 7 * * 1-5", "Jadwal digest (cron, zona Asia/Jakarta)", check=_cron),
    Key("schedule.shift", "list", [], "Jam serah terima shift", check=_shifts),
    Key(
        "api.public_base_url", "str", "", "URL dasar publik untuk tautan; kosong = tautan dimatikan", check=_public_url
    ),
    Key("chat.allow_t2", "bool", False, "Izinkan perintah chat tingkat T2 (berdampak tinggi)"),
    Key(
        "retention.raw_days",
        "int",
        None,
        "Retensi data mentah (hari); wajib diisi sebelum operasi",
        minimum=1,
        maximum=3650,
        nullable=True,
    ),
)

REGISTRY: dict[str, Key] = {k.name: k for k in KEYS}
SECRET_KEYS = frozenset(k.name for k in KEYS if k.secret)


def get(name: str) -> Key | None:
    return REGISTRY.get(name)


def scopes() -> frozenset[str]:
    return frozenset(k.scope for k in KEYS)

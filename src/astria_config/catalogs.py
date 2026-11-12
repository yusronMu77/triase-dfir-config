"""Katalog berversi: hash kanonik, skema ringan per jenis, dan lint (note app-config bagian 5.6).

Satu dokumen JSON utuh per versi. Skema tegas soal kunci tingkat atas yang dikenal tiap jenis dan bentuk entrinya;
isi lain (mis. bobot atau aturan terperinci) dibiarkan longgar sampai lajur pemiliknya menetapkan skema final.
`validate_document` mengembalikan daftar galat (kosong = valid). `lint_references` mengembalikan peringatan lintas
dokumen yang tidak memblokir (dipakai `coverage`).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any, TypeGuard

KINDS = (
    "alert_penjelasan",
    "pola_insiden",
    "temuan_korelasi",
    "langkah",
    "sla",
    "bobot_skor",
    "aturan_android",
    "aturan_peran",
    "aset",
)
SCHEMA_VERSION = "1"
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_STEP_ID = re.compile(r"^S-[A-Za-z0-9_-]+$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def canonical(document: Any) -> bytes:
    """JSON kanonik: kunci terurut, tanpa spasi, UTF-8; NaN dan tak hingga ditolak (`ValueError`)."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def content_hash(document: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(document)).hexdigest()


def value_hash(value: Any) -> str:
    """Hash kanonik nilai pengaturan untuk `before_hash`/`after_hash` di audit (tanpa menyimpan nilainya)."""
    return hashlib.sha256(canonical(value)).hexdigest()


Check = Callable[[Any, str], list[str]]


def _is_str(value: Any, *, empty: bool = False) -> bool:
    return isinstance(value, str) and (empty or bool(value.strip()))


def _str_list(value: Any) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _mapping_entries(
    doc: dict[str, Any], top: str, entry_check: Callable[[str, Any], list[str]], *, id_pattern: re.Pattern[str] = _ID
) -> list[str]:
    entries = doc.get(top)
    if not isinstance(entries, dict) or not entries:
        return [f"{top}: harus objek tidak kosong"]
    errors: list[str] = []
    for ident, entry in entries.items():
        if not id_pattern.match(ident):
            errors.append(f"{top}.{ident}: ID tidak valid")
            continue
        errors.extend(f"{top}.{ident}: {e}" for e in entry_check(ident, entry))
    return errors


def _penjelas(_ident: str, entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return ["harus objek"]
    errors = [f"{f} wajib teks tidak kosong" for f in ("judul", "penjelasan") if not _is_str(entry.get(f))]
    errors += [
        f"{f} harus daftar teks"
        for f in ("penyebab_jinak", "yang_dicek", "rujukan")
        if f in entry and not _str_list(entry[f])
    ]
    return errors


def _pola(_ident: str, entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return ["harus objek"]
    errors = [] if _is_str(entry.get("judul")) else ["judul wajib teks tidak kosong"]
    steps = entry.get("langkah")
    if not _str_list(steps) or not all(_STEP_ID.match(s) for s in steps):
        errors.append("langkah wajib daftar ID langkah (S-...)")
    return errors


def _temuan(_ident: str, entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return ["harus objek"]
    return [] if _is_str(entry.get("judul")) else ["judul wajib teks tidak kosong"]


def _langkah(_ident: str, entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return ["harus objek"]
    errors = [] if _is_str(entry.get("judul")) else ["judul wajib teks tidak kosong"]
    if entry.get("risiko") not in {"aman", "perlu_persetujuan"}:
        errors.append("risiko harus aman atau perlu_persetujuan")
    return errors


def _aset(_ident: str, entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return ["harus objek"]
    if entry.get("kritikalitas") not in {"rendah", "sedang", "tinggi", "kritis"}:
        return ["kritikalitas harus rendah, sedang, tinggi, atau kritis"]
    return []


def _generic_object(_ident: str, entry: Any) -> list[str]:
    return [] if isinstance(entry, dict) else ["harus objek"]


def _sla(_ident: str, entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return ["harus objek"]
    minutes = entry.get("tanggapan_awal_menit")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
        return ["tanggapan_awal_menit harus bilangan bulat positif"]
    return []


def _bobot(doc: dict[str, Any], top: str) -> list[str]:
    weights = doc.get(top)
    if not isinstance(weights, dict) or not weights:
        return [f"{top}: harus objek tidak kosong"]

    def numeric(v: Any) -> bool:
        return isinstance(v, int | float) and not isinstance(v, bool) and v >= 0

    errors = []
    for name, v in weights.items():
        if not (numeric(v) or (isinstance(v, dict) and v and all(numeric(x) for x in v.values()))):
            errors.append(f"{top}.{name}: harus angka >= 0 atau objek angka >= 0")
    return errors


# kind -> (kunci tingkat atas wajib, pemeriksa isi)
_SPECS: dict[str, tuple[str, Callable[[dict[str, Any]], list[str]]]] = {
    "alert_penjelasan": ("entries", lambda d: _mapping_entries(d, "entries", _penjelas)),
    "pola_insiden": ("pola", lambda d: _mapping_entries(d, "pola", _pola)),
    "temuan_korelasi": ("temuan", lambda d: _mapping_entries(d, "temuan", _temuan)),
    "langkah": ("langkah", lambda d: _mapping_entries(d, "langkah", _langkah)),
    "sla": ("band", lambda d: _mapping_entries(d, "band", _sla)),
    "bobot_skor": ("bobot", lambda d: _bobot(d, "bobot")),
    "aturan_android": ("aturan", lambda d: _mapping_entries(d, "aturan", _generic_object)),
    "aturan_peran": ("peran", lambda d: _mapping_entries(d, "peran", _generic_object)),
    "aset": ("aset", lambda d: _mapping_entries(d, "aset", _aset)),
}
_OPTIONAL_TOP = frozenset({"schema_version", "deskripsi"})
_EXTRA_TOP = {"sla": frozenset({"jam_kerja"}), "bobot_skor": frozenset({"ambang"})}


def validate_document(kind: str, document: Any) -> list[str]:
    """Galat skema dokumen (daftar kosong = valid). Pesan menyebut jalur, tidak mengutip isi."""
    if kind not in _SPECS:
        return [f"jenis katalog tidak dikenal: {kind}"]
    if not isinstance(document, dict):
        return ["dokumen harus objek"]
    try:
        raw = canonical(document)
    except TypeError, ValueError:
        return ["dokumen tidak dapat dikanonkan (angka tak hingga atau tipe tidak didukung)"]
    if len(raw) > MAX_DOCUMENT_BYTES:
        return ["dokumen melebihi 2 MiB"]
    top, check = _SPECS[kind]
    allowed = {top} | _OPTIONAL_TOP | _EXTRA_TOP.get(kind, frozenset())
    errors = [f"kunci tingkat atas tidak dikenal: {k}" for k in sorted(set(document) - allowed)]
    if "schema_version" in document and not _is_str(document["schema_version"]):
        errors.append("schema_version harus teks")
    return errors + check(document)


def lint_references(kind: str, document: dict[str, Any], active_langkah: dict[str, Any] | None) -> list[str]:
    """Peringatan lintas dokumen: rujukan langkah yang tidak ada di katalog `langkah` aktif."""
    if kind != "pola_insiden":
        return []
    if active_langkah is None:
        return ["katalog langkah belum aktif: rujukan langkah tidak dapat diperiksa"]
    known = set(active_langkah.get("langkah", {}))
    issues = []
    for ident, entry in sorted(document.get("pola", {}).items()):
        missing = sorted(set(entry.get("langkah", [])) - known)
        if missing:
            issues.append(f"pola {ident}: langkah tidak ada di katalog langkah aktif: {', '.join(missing)}")
    return issues

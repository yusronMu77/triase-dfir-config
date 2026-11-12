<!-- astria-logo -->
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/astria-logo-reverse.svg">
    <img src=".github/assets/astria-logo.svg" alt="Astria" width="320">
  </picture>
</p>

# Astria config

Konfigurasi dan katalog; rahasia tulis-saja.

> Status: Tahap 0, kerangka layanan dari templat `common` v0.1.0, kini memakai `common` v0.2.2 dan `contracts` v0.3.0 (`/health`, `/ready`, `/metrics`, token layanan, Dockerfile, CI). Repo ini adalah submodule dari workspace `triase-dfir` (folder `config/`). Rancangan: `notes/2026-11-06-triase-dfir-app-config.md` di repo root. Lajur developer: 3.

## Kebijakan kanal dan pengaturan (subset identity#4)

Hanya jalur baca `/svc/v1` dan seed dev; endpoint admin, riwayat versi, katalog, rahasia, dan event `config.changed` belum ada.

| Endpoint | Pemanggil (token `svc:config`) | Isi |
|---|---|---|
| `GET /svc/v1/channels/policy` | `api`, `notifier` | `{items: [{channel_id, active, max_detail, band_min}]}` |
| `GET /svc/v1/settings/effective?scope=<scope>` | `api`, `triage`, `ticket`, `scheduler`, `ingest`, `narrative`, `notifier` | `{scope, values: {key: value}}`, tanpa rahasia |

- Tabel `channel_policy` (`max_detail` `minimal|ringkas|penuh`, `band_min` `RENDAH..KRITIS`) dan `setting` (`scope`, `key`, `value` JSONB, `value_version`).
- Seed dev: `ASTRIA_CONFIG_SEED_FILE` berisi `{"channel_policies": [...], "settings": {"<scope>": {"<scope>.<nama>": nilai}}}`. Kunci yang tampak seperti rahasia ditolak. Diterapkan idempoten setelah basis data siap (`value_version` naik hanya bila nilai berubah), `/ready` menunggu seed. Ditolak di `ASTRIA_ENV=prod`. Contoh: `devenv/config/seed.json`.

## Tech stack

Python 3.14, FastAPI, PostgreSQL 18, uv

## Perintah

```text
uv sync                                   # butuh akses ke repo privat contracts dan common (tag git)
uv run ruff format --check . && uv run ruff check .
uv run pytest
sh ../common/template/localtest.sh        # di kontainer py devenv: uji terhadap salinan kerja common dan contracts
```

Menjalankan bersama layanan lain: `pwsh ../devenv/scripts/dev.ps1 up`, lalu `dev.ps1 smoke`.

## Alur git

Branch `{type}/{yyyyMMdd}-{taskId}-{nama}`; commit `{type}({yyyyMMddHHmm}): [{taskId}] {pesan}`, satu baris, tanpa trailer. `main` hanya berubah lewat pull request (merge rebase); jangan push langsung ke `main` dan jangan `--no-verify`.

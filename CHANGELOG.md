# Changelog

## [Unreleased]

### Changed

- `astria-common` naik ke v0.2.3, yang menyematkan `astria-contracts` v0.4.0. Override `[tool.uv]` dihapus.

## [0.2.0] - 2026-11-12

Versi penuh (issue identity#4). Memakai kontrak contracts v0.4.0 (`config-internal` OpenAPI dan event `config.changed`).

### Added

- Registri kunci pengaturan dan lapisan nilai `bawaan < berkas < database < env terkunci` (`ASTRIA_CFG_*`, `ASTRIA_CONFIG_FILE`), divalidasi saat start.
- Rahasia Core tulis-saja: AES-256-GCM, kunci data HKDF-SHA256 dari kunci induk berkas 0600, AAD `nama|versi`; pembacaan khusus `svc:narrative` dicatat ke audit tanpa nilai.
- Aksi tulis dengan audit sinkron fail-closed dan outbox `config.changed` dalam satu transaksi; `if_version` atomik.
- Katalog berversi (sembilan jenis, hash kanonik, draf, tinjau, aktifkan, cakupan) dengan aturan empat mata di kode dan di basis data.
- Kebijakan kanal, penerusan rahasia kanal ke gateway, jalur konsumen `/svc/v1`, metrik Prometheus.
- CLI `config` (`validate`, `catalog lint|hash|seed`, `rotate-master`).
- Uji unit, API (SQLite), dan integrasi PostgreSQL (skema dari kosong, indeks unik parsial, CHECK, konkurensi, rotasi satu transaksi).

### Changed

- Basis data (`ASTRIA_DATABASE_URL`) wajib. Smoke CI memakai SQLite di `/tmp`.
- Dependensi: `astria-common` v0.2.2, `astria-contracts` v0.4.0 (lewat override seperti audit; common v0.2.2 menyematkan v0.3.0), `cryptography`, `pyyaml`.

### Removed

- Kerangka lama `astria_config.api`.

### Added

- Kerangka layanan Tahap 0 dari templat `common` v0.1.0: `/health`, `/ready`, `/metrics`, token layanan, Dockerfile non-root, CI (lint, uji, gitleaks, build image, smoke `/ready`).
- Logo Astria di kepala README (`.github/assets/`, dari kit logo di repo `triase-dfir-uiux`).

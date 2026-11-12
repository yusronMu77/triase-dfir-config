# Changelog

## [Unreleased]

### Added

- Subset identity#4 (issue tetap terbuka): tabel `channel_policy` dan `setting`, `GET /svc/v1/channels/policy` dan `GET /svc/v1/settings/effective?scope=` (token `svc:config`, pemanggil dibatasi), seed dev `ASTRIA_CONFIG_SEED_FILE` (idempoten, ditolak di prod).
- Dependensi `astria-common` v0.2.2 dan `astria-contracts` v0.3.0.
- Kerangka layanan Tahap 0 dari templat `common` v0.1.0: `/health`, `/ready`, `/metrics`, token layanan, Dockerfile non-root, CI (lint, uji, gitleaks, build image, smoke `/ready`).
- Logo Astria di kepala README (`.github/assets/`, dari kit logo di repo `triase-dfir-uiux`).

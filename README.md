<!-- astria-logo -->
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/astria-logo-reverse.svg">
    <img src=".github/assets/astria-logo.svg" alt="Astria" width="320">
  </picture>
</p>

# Astria config

Konfigurasi dan katalog; rahasia tulis-saja.

> Status: versi penuh 0.2.0 (issue identity#4). Repo ini adalah submodule dari workspace `triase-dfir` (folder `config/`). Rancangan: `notes/2026-11-06-triase-dfir-app-config.md` di repo root. Kontrak: `contracts/openapi/config-internal.yaml` dan `contracts/events/config.changed.schema.json` (contracts v0.4.0). Lajur developer: 3.

## Yang dipegang config

| Hal | Jalur | Pemanggil |
|---|---|---|
| Pengaturan runtime (registri kunci, nilai efektif beserta sumber) | `GET /v1/settings`, `PUT /v1/settings/{key}` | `svc:api` dengan `act` admin |
| Rahasia Core (`model.api_key`), tulis-saja | `PUT /v1/secrets/{key}` | admin, re-autentikasi |
| Teruskan rahasia kanal ke gateway (tidak disimpan di Core) | `PUT /v1/secrets/gateway/{key}` | admin, re-autentikasi |
| Kebijakan kanal | `GET /v1/channels/policy`, `PUT /v1/channels/policy/{channel_id}` | admin ubah; admin dan analis baca |
| Katalog berversi (draf, tinjau, aktifkan, cakupan) | `/v1/catalogs/{kind}/versions...`, `/coverage` | analis unggah; admin tinjau dan aktifkan |
| Konsumen layanan inti | `/svc/v1/settings/effective`, `/catalogs/{kind}/active`, `/channels/policy` | layanan inti mana pun |
| Kunci model untuk narasi | `/svc/v1/secrets/model.api_key` | hanya `svc:narrative` |

Urutan setiap aksi tulis yang berdampak: validasi, audit **sinkron** (fail-closed: audit tidak tersedia = 503 `AUDIT_TIDAK_TERSEDIA` dan tidak ada yang berubah), lalu satu transaksi basis data berisi perubahan dan baris outbox `config.changed`. Event tidak pernah memuat nilai. Konkurensi optimistik lewat `if_version` (`UPDATE ... WHERE version = :v`, 409 `VERSI_KONFLIK`).

## Lapisan nilai

`bawaan registri < berkas < database < env (terkunci)`.

- **Berkas**: `ASTRIA_CONFIG_FILE=/etc/astria/config.yaml`, bertingkat `{lingkup: {nama: nilai}}`. Divalidasi saat start; kunci tak dikenal, nilai tak valid, atau rahasia di berkas membuat start ditolak.
- **Database**: diubah admin lewat layar (`PUT /v1/settings/{key}`); ini lapisan yang berubah saat operasi.
- **Env**: `ASTRIA_CFG_<KUNCI>`, titik pada kunci menjadi `__`, huruf besar (`chat.allow_t2` menjadi `ASTRIA_CFG_CHAT__ALLOW_T2`). Bool: `true/false/1/0`; list dan objek: JSON. Kunci yang diisi lewat env tampil `locked` dan ditolak diubah (409 `DIKUNCI_ENV`). Rahasia tidak boleh lewat env.

```yaml
# config.yaml
model:
  timeout_seconds: 30
  allowlist_hosts: ["api.example.com"]
schedule:
  digest: "0 7 * * 1-5"
chat:
  allow_t2: false
```

Kunci terdaftar: `model.chain`, `model.allowlist_hosts`, `model.api_key` (rahasia), `model.timeout_seconds`, `score.band`, `ingest.allowed_roots`, `schedule.digest`, `schedule.shift`, `api.public_base_url`, `chat.allow_t2`, `retention.raw_days`. Sumber kebenarannya `src/astria_config/registry.py`.

## Rahasia

AES-256-GCM. Kunci data diturunkan dengan HKDF-SHA256 dari kunci induk (salt `astria-config`, info `secrets-v{versi kunci}|{nama}`); AAD `nama|versi`, jadi ciphertext terikat ke kunci dan versi nilainya. Nonce 12 byte acak per tulis. Respons, log, event, dan audit tidak pernah memuat nilai (audit juga tanpa hash nilai).

- Kunci induk: 32 byte base64 di `ASTRIA_MASTER_KEY_FILE` (bawaan `/var/lib/astria/config/master.key`, izin 0600). Dibuat otomatis di `dev` dan `test`; di `prod` berkas wajib sudah ada dan start ditolak bila tidak. Pasang volume yang dapat ditulis di jalur itu.
- Ciphertext yang gagal diautentikasi (kunci induk berganti): `GET /svc/v1/secrets/model.api_key` memberi 503 `RAHASIA_TIDAK_TERBACA`; admin menulis ulang nilainya.
- Rotasi: `config rotate-master --new-key-file <jalur>` mengenkripsi ulang semua rahasia dalam satu transaksi (gagal satu = batal semua). Lalu ganti `ASTRIA_MASTER_KEY_FILE` ke berkas baru dan mulai ulang layanan.

## Katalog

Sembilan jenis: `alert_penjelasan`, `pola_insiden`, `temuan_korelasi`, `langkah`, `sla`, `bobot_skor`, `aturan_android`, `aturan_peran`, `aset`. Satu dokumen JSON utuh per versi; `content_hash` = `sha256:` + hex JSON kanonik (kunci terurut, tanpa spasi, UTF-8). Status `draft` → `ditinjau` → `aktif` → `arsip`, satu `aktif` per jenis (ditegakkan indeks unik parsial). **Empat mata**: peninjau dan pengaktif bukan penulis (juga CHECK di basis data untuk peninjau). `GET .../coverage` memberi peringatan, mis. rujukan langkah di `pola_insiden` yang tidak ada di katalog `langkah` aktif.

## Konfigurasi layanan

| Variabel | Arti |
|---|---|
| `ASTRIA_ENV`, `ASTRIA_DATABASE_URL`, `ASTRIA_SERVICE_KEY_FILE` | umum (lihat `astria-common`); basis data wajib |
| `ASTRIA_CONFIG_FILE` | berkas YAML lapisan berkas (opsional) |
| `ASTRIA_MASTER_KEY_FILE` | kunci induk (lihat Rahasia) |
| `ASTRIA_REAUTH_WINDOW_MIN` | umur maksimum `act.reauth_at` (bawaan 10) |
| `ASTRIA_GATEWAY_URL` | tujuan penerusan rahasia kanal (bawaan `http://gateway:8080`) |
| `ASTRIA_SEED_CHANNELS` | daftar JSON kanal yang disemai kebijakannya; bawaan `dev` dan `demo`: `email-tim`, `telegram-tim`, `wa-tim` |

Tanpa kunci layanan, klien audit menolak semua aksi tulis (fail-closed).

## CLI

```text
config validate --file config.yaml                      # berkas dan env ASTRIA_CFG_*; dipakai di CI
config catalog lint <jenis> <berkas.json>               # validasi skema jenis katalog
config catalog hash <berkas.json>                       # content_hash kanonik
config catalog seed <jenis> <berkas.json> --by <nama>   # bootstrap dev: langsung aktif; ditolak di prod
config rotate-master --new-key-file <jalur>
```

Metrik: `config_changes_total{scope}`, `config_audit_failures_total`, `config_catalog_active_version{kind}`, `config_secret_reads_total{result}`, `config_secret_unreadable_total`, `config_auth_rejections_total{reason}`.

## Tech stack

Python 3.14, FastAPI, SQLAlchemy Core (PostgreSQL 18 di server, SQLite untuk uji dan lokal), cryptography (AES-GCM, HKDF), uv

## Perintah

```text
uv sync                                   # butuh akses ke repo privat contracts dan common (tag git)
uv run ruff format --check . && uv run ruff check .
uv run pytest                             # SQLite; uji PostgreSQL ikut jalan bila ASTRIA_TEST_DATABASE_URL diisi
sh ../common/template/localtest.sh        # di kontainer py devenv: uji terhadap salinan kerja common dan contracts
```

Menjalankan bersama layanan lain: `pwsh ../devenv/scripts/dev.ps1 up`, lalu `dev.ps1 smoke`.

## Alur git

Branch `{type}/{yyyyMMdd}-{taskId}-{nama}`; commit `{type}({yyyyMMddHHmm}): [{taskId}] {pesan}`, satu baris, tanpa trailer. `main` hanya berubah lewat pull request (merge rebase); jangan push langsung ke `main` dan jangan `--no-verify`.

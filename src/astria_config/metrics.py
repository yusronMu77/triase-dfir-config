"""Metrik Prometheus config. Label tidak pernah memuat nilai pengaturan atau rahasia."""

from prometheus_client import Counter, Gauge

CHANGES = Counter("config_changes_total", "Perubahan yang berlaku, per lingkup", ["scope"])
AUDIT_FAILURES = Counter("config_audit_failures_total", "Audit sinkron gagal sehingga perubahan ditolak")
CATALOG_ACTIVE = Gauge("config_catalog_active_version", "Versi katalog aktif per jenis (0 = belum ada)", ["kind"])
SECRET_READS = Counter("config_secret_reads_total", "Pembacaan rahasia lewat /svc/v1", ["result"])
SECRET_UNREADABLE = Counter("config_secret_unreadable_total", "Rahasia gagal diautentikasi AES-GCM (curiga)")
AUTH_REJECTIONS = Counter("config_auth_rejections_total", "Pemanggil admin ditolak", ["reason"])

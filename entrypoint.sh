#!/usr/bin/env bash
# =============================================================================
# entrypoint.sh — запуск PostgreSQL в foreground
# =============================================================================
set -euo pipefail

PG_INSTALL_DIR="${PG_INSTALL_DIR:-/usr/local/pgsql}"
PG_DATA="${PG_DATA:-/var/lib/postgresql/data}"

# Разрешение подключаться со всех портов ез паролей (для тестов снаружи)
echo "host all all 0.0.0.0/0 trust" >> "${PG_DATA}/pg_hba.conf"

exec "${PG_INSTALL_DIR}/bin/postgres" -D "${PG_DATA}" "$@"
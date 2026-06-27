#!/usr/bin/env bash
# =============================================================================
# entrypoint.sh — запуск PostgreSQL в foreground
# =============================================================================
set -euo pipefail

PG_INSTALL_DIR="${PG_INSTALL_DIR:-/usr/local/pgsql}"
PG_DATA="${PG_DATA:-/var/lib/postgresql/data}"

exec "${PG_INSTALL_DIR}/bin/postgres" -D "${PG_DATA}" "$@"
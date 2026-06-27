#!/usr/bin/env bash
# =============================================================================
# install.sh — установка расширения pg_deadlock_log
# Запускается от пользователя postgres
# =============================================================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# -----------------------------------------------------------------------------
# Аргументы
# -----------------------------------------------------------------------------
PG_SRC=""
EXT_DIR=""
PG_DATA=""
TARGET_DB="postgres"

usage() {
    echo "Usage: $0 --pg-src <path> --ext-dir <path> --pg-data <path> [--db <dbname>]"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pg-src)   PG_SRC="$2";    shift 2 ;;
        --ext-dir)  EXT_DIR="$2";   shift 2 ;;
        --pg-data)  PG_DATA="$2";   shift 2 ;;
        --db)       TARGET_DB="$2"; shift 2 ;;
        *) log_error "Unknown argument: $1"; usage ;;
    esac
done

# -----------------------------------------------------------------------------
# Валидация
# -----------------------------------------------------------------------------
[[ -z "$PG_SRC"  ]] && { log_error "--pg-src is required";  usage; }
[[ -z "$EXT_DIR" ]] && { log_error "--ext-dir is required"; usage; }
[[ -z "$PG_DATA" ]] && { log_error "--pg-data is required"; usage; }

[[ -d "$PG_SRC"  ]] || { log_error "PG_SRC not found: $PG_SRC";   exit 1; }
[[ -d "$EXT_DIR" ]] || { log_error "EXT_DIR not found: $EXT_DIR"; exit 1; }
[[ -d "$PG_DATA" ]] || { log_error "PG_DATA not found: $PG_DATA"; exit 1; }

# -----------------------------------------------------------------------------
# pg_config
# -----------------------------------------------------------------------------
PG_CONFIG=$(command -v pg_config || true)
[[ -z "$PG_CONFIG" ]] && { log_error "pg_config not found in PATH (${PATH})"; exit 1; }

PG_SHAREDIR=$("$PG_CONFIG" --sharedir)
PG_PKGLIBDIR=$("$PG_CONFIG" --pkglibdir)
PG_VERSION=$("$PG_CONFIG" --version)

log_info "PostgreSQL : ${PG_VERSION}"
log_info "sharedir   : ${PG_SHAREDIR}"
log_info "pkglibdir  : ${PG_PKGLIBDIR}"
log_info "PG_SRC     : ${PG_SRC}"
log_info "EXT_DIR    : ${EXT_DIR}"
log_info "TARGET_DB  : ${TARGET_DB}"

# -----------------------------------------------------------------------------
# Шаг 1: Копируем изменённые файлы ядра PG
# -----------------------------------------------------------------------------
log_info "Copying patched PostgreSQL source files..."

PATCHED_DEADLOCK="${EXT_DIR}/postgres/src/backend/storage/lmgr/deadlock.c"
PATCHED_LOCK_H="${EXT_DIR}/postgres/src/include/storage/lock.h"

[[ -f "$PATCHED_DEADLOCK" ]] || { log_error "Patched file not found: $PATCHED_DEADLOCK"; exit 1; }
[[ -f "$PATCHED_LOCK_H"   ]] || { log_error "Patched file not found: $PATCHED_LOCK_H";   exit 1; }

cp "$PATCHED_DEADLOCK" "${PG_SRC}/src/backend/storage/lmgr/deadlock.c"
cp "$PATCHED_LOCK_H"   "${PG_SRC}/src/include/storage/lock.h"

log_info "Patched files copied successfully"

# -----------------------------------------------------------------------------
# Шаг 2: Пересобираем PG (патч мог затронуть заголовки)
# -----------------------------------------------------------------------------
log_info "Rebuilding PostgreSQL after patch..."
make -C "$PG_SRC" -j"$(nproc)"
make -C "$PG_SRC" install
log_info "PostgreSQL rebuild complete"

# -----------------------------------------------------------------------------
# Шаг 3: Собираем и устанавливаем расширение
# -----------------------------------------------------------------------------
log_info "Building extension..."
make -C "$EXT_DIR" PG_CONFIG="$PG_CONFIG"
make -C "$EXT_DIR" PG_CONFIG="$PG_CONFIG" install
log_info "Extension installed"

# -----------------------------------------------------------------------------
# Шаг 4: shared_preload_libraries
# -----------------------------------------------------------------------------
PG_CONF="${PG_DATA}/postgresql.conf"

if grep -q "^shared_preload_libraries" "$PG_CONF"; then
    CURRENT=$(grep "^shared_preload_libraries" "$PG_CONF" \
              | sed "s/shared_preload_libraries\s*=\s*['\"]//;s/['\"].*//")
    if echo "$CURRENT" | grep -q "pg_deadlock_log"; then
        log_warn "pg_deadlock_log already in shared_preload_libraries"
    else
        if [[ -z "$CURRENT" ]]; then
            NEW_VAL="pg_deadlock_log"
        else
            NEW_VAL="${CURRENT},pg_deadlock_log"
        fi
        sed -i "s|^shared_preload_libraries.*|shared_preload_libraries = '${NEW_VAL}'|" "$PG_CONF"
        log_info "shared_preload_libraries = '${NEW_VAL}'"
    fi
else
    echo "shared_preload_libraries = 'pg_deadlock_log'" >> "$PG_CONF"
    log_info "Added shared_preload_libraries = 'pg_deadlock_log'"
fi

log_info "install.sh done. Restart PostgreSQL to apply changes."
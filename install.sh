#!/bin/bash
set -e

# ============================================================
# deploy_pg_deadlock_log.sh
# Использование:
#   ./deploy_pg_deadlock_log.sh \
#     --pg-src /path/to/postgresql-source \
#     --db-name mydb \
#     [--pg-user postgres] \
#     [--clone-dir /tmp/pg_deadlock_log]
# ============================================================

# --- Значения по умолчанию ---
PG_USER="postgres"
CLONE_DIR="/tmp/pg_deadlock_log"
DB_NAME=""
PG_SRC=""
REPO_URL="https://github.com/khlyupinivan/pg_deadlock_log.git"

# --- Парсинг аргументов ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --pg-src)    PG_SRC="$2";    shift 2 ;;
        --db-name)   DB_NAME="$2";   shift 2 ;;
        --pg-user)   PG_USER="$2";   shift 2 ;;
        --clone-dir) CLONE_DIR="$2"; shift 2 ;;
        *)
            echo "Неизвестный параметр: $1"
            echo "Использование: $0 --pg-src <путь> --db-name <бд> [--pg-user postgres] [--clone-dir /tmp/pg_deadlock_log]"
            exit 1
            ;;
    esac
done

# --- Проверка обязательных параметров ---
if [[ -z "$PG_SRC" ]]; then
    echo "Ошибка: не указан --pg-src (путь к исходникам PostgreSQL)"
    exit 1
fi
if [[ -z "$DB_NAME" ]]; then
    echo "Ошибка: не указан --db-name (имя базы данных)"
    exit 1
fi
if [[ ! -d "$PG_SRC" ]]; then
    echo "Ошибка: директория исходников PostgreSQL не найдена: $PG_SRC"
    exit 1
fi

# --- Пути ---
PG_BIN=$(pg_config --bindir)
PG_LIB=$(pg_config --pkglibdir)
PG_SHARE=$(pg_config --sharedir)
PG_DATA=$(psql -U "$PG_USER" -tAc "SHOW data_directory;")
PG_CONF="$PG_DATA/postgresql.conf"

echo "=== Параметры развёртки ==="
echo "  PG_SRC    : $PG_SRC"
echo "  PG_BIN    : $PG_BIN"
echo "  PG_LIB    : $PG_LIB"
echo "  PG_SHARE  : $PG_SHARE"
echo "  PG_DATA   : $PG_DATA"
echo "  DB_NAME   : $DB_NAME"
echo "  PG_USER   : $PG_USER"
echo "  CLONE_DIR : $CLONE_DIR"
echo ""

# ============================================================
# ШАГ 1: Клонирование репозитория
# ============================================================
echo "=== Шаг 1: Клонирование репозитория ==="

if [[ -d "$CLONE_DIR" ]]; then
    echo "Директория $CLONE_DIR уже существует — обновляем..."
    cd "$CLONE_DIR"
    git pull origin main
else
    git clone "$REPO_URL" "$CLONE_DIR"
    cd "$CLONE_DIR"
fi

# ============================================================
# ШАГ 2: Замена файлов ядра PostgreSQL
# ============================================================
echo ""
echo "=== Шаг 2: Замена файлов ядра PostgreSQL ==="

DEADLOCK_C_SRC="$CLONE_DIR/postgres/src/backend/storage/lmgr/deadlock.c"
LOCK_H_SRC="$CLONE_DIR/postgres/src/include/storage/lock.h"

DEADLOCK_C_DST="$PG_SRC/src/backend/storage/lmgr/deadlock.c"
LOCK_H_DST="$PG_SRC/src/include/storage/lock.h"

# Бэкап оригиналов (только если бэкап ещё не делался)
if [[ ! -f "${DEADLOCK_C_DST}.orig" ]]; then
    echo "Бэкап: $DEADLOCK_C_DST -> ${DEADLOCK_C_DST}.orig"
    cp "$DEADLOCK_C_DST" "${DEADLOCK_C_DST}.orig"
fi
if [[ ! -f "${LOCK_H_DST}.orig" ]]; then
    echo "Бэкап: $LOCK_H_DST -> ${LOCK_H_DST}.orig"
    cp "$LOCK_H_DST" "${LOCK_H_DST}.orig"
fi

# Копирование изменённых файлов
echo "Копируем deadlock.c..."
cp "$DEADLOCK_C_SRC" "$DEADLOCK_C_DST"

echo "Копируем lock.h..."
cp "$LOCK_H_SRC" "$LOCK_H_DST"

# ============================================================
# ШАГ 3: Пересборка модуля lmgr
# ============================================================
echo ""
echo "=== Шаг 3: Пересборка модуля lmgr ==="

cd "$PG_SRC"
make -C src/backend/storage/lmgr
make -C src/backend/storage/lmgr install

# ============================================================
# ШАГ 4: Остановка PostgreSQL
# ============================================================
echo ""
echo "=== Шаг 4: Остановка PostgreSQL ==="

"$PG_BIN/pg_ctl" stop -D "$PG_DATA" -m fast
echo "PostgreSQL остановлен."

# ============================================================
# ШАГ 5: Компиляция и установка расширения
# ============================================================
echo ""
echo "=== Шаг 5: Компиляция и установка расширения ==="

cd "$CLONE_DIR"
make USE_PGXS=1
make USE_PGXS=1 install

echo "Расширение установлено:"
echo "  $PG_LIB/pg_deadlock_log.so"
echo "  $PG_SHARE/extension/pg_deadlock_log.control"

# ============================================================
# ШАГ 6: Добавление в shared_preload_libraries
# ============================================================
echo ""
echo "=== Шаг 6: Настройка shared_preload_libraries ==="

# Читаем текущее значение (убираем комментарии и пробелы)
CURRENT_SPL=$(grep -E "^\s*shared_preload_libraries\s*=" "$PG_CONF" \
    | tail -1 \
    | sed "s/.*=\s*//; s/'//g; s/\"//g; s/#.*//" \
    | tr -d ' ')

if [[ -z "$CURRENT_SPL" ]]; then
    # Параметра нет — добавляем строку
    echo "shared_preload_libraries = 'pg_deadlock_log'" >> "$PG_CONF"
    echo "Добавлено: shared_preload_libraries = 'pg_deadlock_log'"
elif echo "$CURRENT_SPL" | grep -q "pg_deadlock_log"; then
    echo "pg_deadlock_log уже присутствует в shared_preload_libraries — пропускаем."
else
    # Есть другие расширения — дописываем через запятую
    NEW_SPL="${CURRENT_SPL},pg_deadlock_log"
    # Заменяем строку в конфиге
    sed -i "s|^\s*shared_preload_libraries\s*=.*|shared_preload_libraries = '$NEW_SPL'|" "$PG_CONF"
    echo "Обновлено: shared_preload_libraries = '$NEW_SPL'"
fi

# ============================================================
# ШАГ 7: Запуск PostgreSQL
# ============================================================
echo ""
echo "=== Шаг 7: Запуск PostgreSQL ==="

"$PG_BIN/pg_ctl" start -D "$PG_DATA"
echo "Ожидаем готовности PostgreSQL..."
sleep 3

# ============================================================
# ШАГ 8: CREATE EXTENSION в целевой БД
# ============================================================
echo ""
echo "=== Шаг 8: Создание расширения в базе '$DB_NAME' ==="

psql -U "$PG_USER" -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS pg_deadlock_log;"

echo ""
echo "=== Проверка ==="
psql -U "$PG_USER" -d "$DB_NAME" -c "\dx pg_deadlock_log"
psql -U "$PG_USER" -d "$DB_NAME" -c "SELECT COUNT(*) FROM pg_deadlock_log;"

echo ""
echo "=== Развёртка завершена успешно ==="
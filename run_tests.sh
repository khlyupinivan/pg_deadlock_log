#!/usr/bin/env bash
set -uo pipefail

# Каталог проекта = каталог этого скрипта
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# ── DSN ──────────────────────────────────────────────────────────────────────
: "${PG_DEADLOCK_LOG_DSN:=dbname=deadlock_test user=ivan}"
export PG_DEADLOCK_LOG_DSN

: "${PG_DEADLOCK_LOG_DSN_TCP:=dbname=deadlock_test user=ivan host=127.0.0.1 port=5432 password=deadlock_test}"
export PG_DEADLOCK_LOG_DSN_TCP

echo "DSN      : ${PG_DEADLOCK_LOG_DSN}"
echo "DSN (TCP): ${PG_DEADLOCK_LOG_DSN_TCP}"

# ── Проверяем доступность PostgreSQL ─────────────────────────────────────────
if ! psql "${PG_DEADLOCK_LOG_DSN}" -c "SELECT 1;" > /dev/null 2>&1; then
  echo "ERROR: PostgreSQL недоступен по DSN='${PG_DEADLOCK_LOG_DSN}'"
  exit 1
fi

# ── Виртуальное окружение ─────────────────────────────────────────────────────
if [[ ! -d "venv" ]]; then
  echo "ERROR: virtualenv ./venv не найден."
  echo "Создайте и установите зависимости:"
  echo "  python3 -m venv venv"
  echo "  source venv/bin/activate"
  echo "  pip install -r requirements.txt"
  echo "  deactivate"
  exit 1
fi

source venv/bin/activate

# ── Разбираем аргументы ───────────────────────────────────────────────────────
RUN_TCP=0
PYTEST_EXTRA_ARGS=()

for arg in "$@"; do
  case "$arg" in
    --tcp)
      RUN_TCP=1
      ;;
    *)
      PYTEST_EXTRA_ARGS+=("$arg")
      ;;
  esac
done

# ── Собираем аргументы pytest ─────────────────────────────────────────────────
PYTEST_ARGS=(-v tests)

if [[ "${RUN_TCP}" -eq 1 ]]; then
  PYTEST_ARGS+=(-m "requires_tcp or not requires_tcp")
else
  PYTEST_ARGS+=(-m "not requires_tcp")
fi

PYTEST_ARGS+=("${PYTEST_EXTRA_ARGS[@]}")

# ── Запускаем pytest, сохраняем код возврата ─────────────────────────────────
pytest "${PYTEST_ARGS[@]}"
EXIT_CODE=$?

# ── Деактивируем окружение всегда ────────────────────────────────────────────
deactivate

exit "${EXIT_CODE}"
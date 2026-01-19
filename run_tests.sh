#!/usr/bin/env bash
set -euo pipefail

# Каталог проекта = каталог этого скрипта
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Настраиваем DSN, если не задан
: "${PG_DEADLOCK_LOG_DSN:=dbname=deadlock_test user=ivan}"
export PG_DEADLOCK_LOG_DSN

# Отдельный DSN для TCP-тестов (с хостом и паролем)
: "${PG_DEADLOCK_LOG_DSN_TCP:=dbname=deadlock_test user=ivan host=127.0.0.1 port=5432 password=deadlock_test}"
export PG_DEADLOCK_LOG_DSN_TCP

# Активируем виртуальное окружение
if [[ ! -d "venv" ]]; then
  echo "Virtualenv ./venv not found."
  echo "Create and install deps:"
  echo "  python3 -m venv venv"
  echo "  source venv/bin/activate"
  echo "  pip install -r requirements.txt"
  echo "  deactivate"
  exit 1
fi

source venv/bin/activate

# Запускаем pytest
pytest -q tests

# Деактивируем окружение
deactivate
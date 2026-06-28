#!/usr/bin/env python3
"""
tests/stress_test.py
Стресс-тест для pg_deadlock_log: N параллельных дедлоков одновременно.
Запускается STRESS_RUNS раз, считает суммарную статистику потерь.
"""

import time
import threading
import psycopg2
import psycopg2.extensions
from datetime import datetime, timezone

# =============================================================================
# Конфигурация
# =============================================================================
DSN            = "host=localhost port=5432 dbname=postgres user=postgres"
DEADLOCK_COUNT = 1     # параллельных дедлоков за один прогон
STRESS_RUNS    = 100    # сколько раз повторяем стресс-тест
WAIT_TIMEOUT   = 10     # таймаут ожидания записей от воркера
WAIT_INTERVAL  = 1      # интервал проверки

# =============================================================================
# Утилиты
# =============================================================================
def new_conn(autocommit=False):
    conn = psycopg2.connect(DSN)
    conn.autocommit = autocommit
    return conn

def wait_for_records(since: datetime, expected_count: int) -> list:
    deadline = time.time() + WAIT_TIMEOUT
    conn = new_conn(autocommit=True)
    cur = conn.cursor()

    best_rows = []

    while time.time() < deadline:
        cur.execute("""
            SELECT id, occurred_at, pid_victim
            FROM pg_deadlock_log
            WHERE occurred_at >= %s
            ORDER BY occurred_at
        """, (since,))
        rows = cur.fetchall()

        if len(rows) > len(best_rows):
            best_rows = rows

        if len(best_rows) >= expected_count:
            break

        time.sleep(WAIT_INTERVAL)

    cur.close()
    conn.close()
    return best_rows

# =============================================================================
# Подготовка
# =============================================================================
def setup():
    print("\n=== SETUP ===")
    conn = new_conn(autocommit=True)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deadlock_test (
            id  INTEGER PRIMARY KEY,
            val TEXT
        )
    """)
    cur.execute("TRUNCATE deadlock_test")
    values = ", ".join(f"({i}, 'v{i}')" for i in range(1, DEADLOCK_COUNT * 2 + 1))
    cur.execute(f"INSERT INTO deadlock_test VALUES {values}")
    cur.close()
    conn.close()
    print(f"  Таблица deadlock_test создана и заполнена ({DEADLOCK_COUNT * 2} строк)")

def reset_rows():
    """Сбрасываем val перед каждым прогоном чтобы строки были актуальны."""
    conn = new_conn(autocommit=True)
    cur = conn.cursor()
    for i in range(1, DEADLOCK_COUNT * 2 + 1):
        cur.execute("UPDATE deadlock_test SET val = %s WHERE id = %s", (f'v{i}', i))
    cur.close()
    conn.close()

# =============================================================================
# Один параллельный дедлок
# =============================================================================
def cause_deadlock_parallel(index: int):
    row_a = index * 2 - 1
    row_b = index * 2
    barrier = threading.Barrier(2)

    def session_a():
        conn = new_conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE deadlock_test SET val = 'a' WHERE id = %s", (row_a,))
            barrier.wait()
            time.sleep(0.1)
            cur.execute("UPDATE deadlock_test SET val = 'a' WHERE id = %s", (row_b,))
            conn.commit()
        except psycopg2.errors.DeadlockDetected:
            conn.rollback()
        except Exception:
            conn.rollback()
        finally:
            conn.close()

    def session_b():
        conn = new_conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE deadlock_test SET val = 'b' WHERE id = %s", (row_b,))
            barrier.wait()
            time.sleep(0.1)
            cur.execute("UPDATE deadlock_test SET val = 'b' WHERE id = %s", (row_a,))
            conn.commit()
        except psycopg2.errors.DeadlockDetected:
            conn.rollback()
        except Exception:
            conn.rollback()
        finally:
            conn.close()

    t_a = threading.Thread(target=session_a)
    t_b = threading.Thread(target=session_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)

# =============================================================================
# Один прогон стресс-теста
# =============================================================================
def run_once(run_index: int) -> tuple[int, int]:
    """Возвращает (найдено, ожидалось)."""
    reset_rows()

    started_at = datetime.now(timezone.utc)
    threads = [
        threading.Thread(target=cause_deadlock_parallel, args=(i,))
        for i in range(1, DEADLOCK_COUNT + 1)
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    records = wait_for_records(started_at, DEADLOCK_COUNT)
    found = len(records)

    status = "OK  " if found >= DEADLOCK_COUNT else f"LOSS"
    lost   = DEADLOCK_COUNT - found
    print(f"  Прогон {run_index:3d}/{STRESS_RUNS}  [{status}]  "
          f"найдено: {found:2d}/{DEADLOCK_COUNT}"
          + (f"  потеряно: {lost}" if lost > 0 else ""))

    return found, DEADLOCK_COUNT

# =============================================================================
# Стресс-тест
# =============================================================================
def test_stress():
    print(f"\n=== СТРЕСС-ТЕСТ: {STRESS_RUNS} прогонов × {DEADLOCK_COUNT} параллельных дедлоков ===\n")

    total_found    = 0
    total_expected = 0
    perfect_runs   = 0
    loss_runs      = 0

    for i in range(1, STRESS_RUNS + 1):
        found, expected = run_once(i)
        total_found    += found
        total_expected += expected
        if found >= expected:
            perfect_runs += 1
        else:
            loss_runs += 1

    total_lost = total_expected - total_found

    print(f"\n=== ИТОГ ===")
    print(f"  Прогонов всего:       {STRESS_RUNS}")
    print(f"  Прогонов без потерь:  \033[32m{perfect_runs}\033[0m")
    print(f"  Прогонов с потерями:  \033[{'31' if loss_runs > 0 else '32'}m{loss_runs}\033[0m")
    print(f"  Дедлоков ожидалось:   {total_expected}")
    print(f"  Дедлоков поймано:     {total_found}")
    print(f"  Дедлоков потеряно:    \033[{'31' if total_lost > 0 else '32'}m{total_lost}\033[0m  "
          f"({total_lost / total_expected * 100:.1f}%)")

    if loss_runs == 0:
        print(f"\n  \033[32m✓ shm справляется с нагрузкой — ring buffer не нужен\033[0m")
    else:
        print(f"\n  \033[31m✗ однослотовый shm теряет записи — нужен ring buffer\033[0m")

# =============================================================================
# Точка входа
# =============================================================================
if __name__ == "__main__":
    import sys
    setup()
    test_stress()
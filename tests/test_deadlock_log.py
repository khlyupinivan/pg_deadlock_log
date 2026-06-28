#!/usr/bin/env python3
"""
tests/test_deadlock_log.py
Тесты для расширения pg_deadlock_log.
Запускается снаружи контейнера, подключается к PG через psycopg2.
"""

import time
import threading
import psycopg2
import psycopg2.extensions
from datetime import datetime, timezone

# =============================================================================
# Конфигурация
# =============================================================================
DSN = "host=localhost port=5432 dbname=postgres user=postgres"
DEADLOCK_COUNT = 3       # сколько раз провоцируем дедлок
DEADLOCK_PAUSE = 0.0     # пауза между дедлоками (секунды)
WAIT_TIMEOUT   = 60      # максимум секунд ожидания записей от воркера
WAIT_INTERVAL  = 2       # интервал проверки (секунды)

# =============================================================================
# Утилиты
# =============================================================================
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []

def check(name: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    msg = f"  [{status}] {name}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    results.append((name, condition))

def new_conn(autocommit=False):
    conn = psycopg2.connect(DSN)
    conn.autocommit = autocommit
    return conn

def wait_for_records(since: datetime, expected_count: int) -> list:
    print(f"\n  Ожидаем {expected_count} записей от воркера (таймаут {WAIT_TIMEOUT}с)...")
    deadline = time.time() + WAIT_TIMEOUT
    conn = new_conn(autocommit=True)
    cur = conn.cursor()

    best_rows = []
    best_cols = []

    while time.time() < deadline:
        cur.execute("""
            SELECT
                id, occurred_at, database_name, user_name,
                pid_victim, sqlstate, query, error_message,
                error_detail, application_name, client_addr,
                search_path, xid, virtualxid, all_pids, lock_cycle
            FROM pg_deadlock_log
            WHERE occurred_at >= %s
            ORDER BY occurred_at
        """, (since,))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        elapsed = WAIT_TIMEOUT - (deadline - time.time())
        print(f"  [{elapsed:.0f}с] Найдено записей: {len(rows)}/{expected_count}")

        if len(rows) > len(best_rows):
            best_rows = rows   # ← запоминаем лучший результат
            best_cols = cols

        if len(best_rows) >= expected_count:
            break              # ← нашли все, выходим сразу

        time.sleep(WAIT_INTERVAL)

    cur.close()
    conn.close()

    # ← возвращаем лучшее что нашли, а не [] при таймауте
    return [dict(zip(best_cols, row)) for row in best_rows] if best_rows else []

# =============================================================================
# Подготовка: создаём таблицу для тестов
# =============================================================================
def setup():
    print("\n=== SETUP ===")
    conn = new_conn(autocommit=True)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deadlock_test (
            id   INTEGER PRIMARY KEY,
            val  TEXT
        )
    """)
    cur.execute("TRUNCATE deadlock_test")
    cur.execute("INSERT INTO deadlock_test VALUES (1, 'a'), (2, 'b')")
    cur.close()
    conn.close()
    print("  Таблица deadlock_test создана и заполнена")

# =============================================================================
# Провоцируем один дедлок
# =============================================================================
def cause_deadlock(index: int) -> datetime:
    """
    Сессия A: блокирует строку 1, затем пытается заблокировать строку 2.
    Сессия B: блокирует строку 2, затем пытается заблокировать строку 1.
    Один из них получит DeadlockDetected.
    Возвращает время начала дедлока.
    """
    barrier = threading.Barrier(2)
    started_at = datetime.now(timezone.utc)
    errors = []

    def session_a():
        conn = new_conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE deadlock_test SET val = 'a_new' WHERE id = 1")
            barrier.wait()          # ждём пока B заблокирует строку 2
            time.sleep(0.1)
            cur.execute("UPDATE deadlock_test SET val = 'a_new' WHERE id = 2")
            conn.commit()
        except psycopg2.errors.DeadlockDetected:
            conn.rollback()
        except Exception as e:
            errors.append(f"session_a: {e}")
            conn.rollback()
        finally:
            conn.close()

    def session_b():
        conn = new_conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE deadlock_test SET val = 'b_new' WHERE id = 2")
            barrier.wait()          # ждём пока A заблокирует строку 1
            time.sleep(0.1)
            cur.execute("UPDATE deadlock_test SET val = 'b_new' WHERE id = 1")
            conn.commit()
        except psycopg2.errors.DeadlockDetected:
            conn.rollback()
        except Exception as e:
            errors.append(f"session_b: {e}")
            conn.rollback()
        finally:
            conn.close()

    t_a = threading.Thread(target=session_a)
    t_b = threading.Thread(target=session_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)

    if errors:
        raise RuntimeError(f"Deadlock #{index} unexpected errors: {errors}")

    return started_at

# =============================================================================
# Основные тесты
# =============================================================================
def test_deadlocks():
    print(f"\n=== ПРОВОЦИРУЕМ {DEADLOCK_COUNT} ДЕДЛОКА(ОВ) ===")

    first_started_at = None
    deadlock_times = []

    for i in range(1, DEADLOCK_COUNT + 1):
        print(f"  Дедлок {i}/{DEADLOCK_COUNT}...")
        started_at = cause_deadlock(i)
        print(f"    спровоцирован в: {started_at.isoformat()}")  # ← добавь это
        deadlock_times.append(started_at)
        if first_started_at is None:
            first_started_at = started_at
        if i < DEADLOCK_COUNT:
            time.sleep(DEADLOCK_PAUSE)

    # Ждём пока воркер запишет все дедлоки
    records = wait_for_records(first_started_at, DEADLOCK_COUNT)

    print("\n=== ПРОВЕРКА ЗАПИСЕЙ ===")

    # Тест 1: количество пойманных дедлоков
    check(
        f"Пойманы все {DEADLOCK_COUNT} дедлока(ов)",
        len(records) >= DEADLOCK_COUNT,
        f"Найдено записей: {len(records)}, ожидалось: {DEADLOCK_COUNT}"
    )

    for i, r in enumerate(records, 1):
        print(f"\n  --- Дедлок #{i} (id={r['id']}) ---")

        check("occurred_at заполнен", r["occurred_at"] is not None)
        check("occurred_at не в будущем", r["occurred_at"] <= datetime.now(timezone.utc), f"occurred_at = {r['occurred_at']}")
        check("database_name = 'postgres'", r["database_name"] == "postgres", f"database_name = {r['database_name']!r}")
        check("user_name заполнен", r["user_name"] is not None and r["user_name"] != "", f"user_name = {r['user_name']!r}")
        check("pid_victim > 0", r["pid_victim"] is not None and r["pid_victim"] > 0, f"pid_victim = {r['pid_victim']}")
        check("sqlstate = '40P01'", r["sqlstate"] == "40P01", f"sqlstate = {r['sqlstate']!r}")
        check("error_message заполнен", r["error_message"] is not None and r["error_message"] != "", f"error_message = {r['error_message']!r}")
        check("query заполнен", r["query"] is not None and r["query"] != "", f"query = {r['query']!r}")
        check("all_pids содержит >= 2 участников", r["all_pids"] is not None and len(r["all_pids"]) >= 2, f"all_pids = {r['all_pids']}")
        check("pid_victim входит в all_pids", r["pid_victim"] in (r["all_pids"] or []), f"pid_victim={r['pid_victim']}, all_pids={r['all_pids']}")
        check("lock_cycle заполнен", r["lock_cycle"] is not None and r["lock_cycle"] != "", f"lock_cycle = {r['lock_cycle']!r}")
        check("xid или virtualxid заполнен", r["xid"] is not None or r["virtualxid"] is not None, f"xid={r['xid']}, virtualxid={r['virtualxid']!r}")

# =============================================================================
# Итог
# =============================================================================
def print_summary():
    print("\n=== ИТОГ ===")
    total  = len(results)
    passed = sum(1 for _, ok in results if ok)
    failed = total - passed
    print(f"  Всего:  {total}")
    print(f"  \033[32mПройдено: {passed}\033[0m")
    if failed:
        print(f"  \033[31mПровалено: {failed}\033[0m")
        for name, ok in results:
            if not ok:
                print(f"    - {name}")
    return failed == 0

# =============================================================================
# Точка входа
# =============================================================================
if __name__ == "__main__":
    import sys
    setup()
    test_deadlocks()
    ok = print_summary()
    sys.exit(0 if ok else 1)
#!/usr/bin/env python3
"""
tests/test_deadlock_scenarios.py

Тесты всех сценариев дедлоков для pg_deadlock_log.
Сценарии:
  1. Row-level дедлок (2 сессии, 2 строки)
  2. Table-level lock дедлок (LOCK TABLE)
  3. Advisory locks дедлок (pg_advisory_lock)
  4. Дедлок из 3 участников (цепочка A→B→C→A)
  5. store_query = off — поле query должно быть NULL
  6. enabled = off — записей не должно появляться
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
WAIT_TIMEOUT   = 30
WAIT_INTERVAL  = 2
DEADLOCK_PAUSE = 0.1

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
            best_rows = rows
            best_cols = cols

        if len(best_rows) >= expected_count:
            break

        time.sleep(WAIT_INTERVAL)

    cur.close()
    conn.close()
    return [dict(zip(best_cols, row)) for row in best_rows] if best_rows else []

def set_guc(name: str, value: str):
    """Устанавливает GUC через ALTER SYSTEM и перечитывает конфиг."""
    conn = new_conn(autocommit=True)
    cur = conn.cursor()
    cur.execute(f"ALTER SYSTEM SET {name} = '{value}'")
    cur.execute("SELECT pg_reload_conf()")
    cur.close()
    conn.close()
    time.sleep(0.5)

def reset_guc(name: str):
    """Сбрасывает GUC к значению по умолчанию."""
    conn = new_conn(autocommit=True)
    cur = conn.cursor()
    cur.execute(f"ALTER SYSTEM RESET {name}")
    cur.execute("SELECT pg_reload_conf()")
    cur.close()
    conn.close()
    time.sleep(0.5)

def check_base_fields(r: dict):
    """Проверяет базовые поля записи о дедлоке."""
    check("occurred_at заполнен", r["occurred_at"] is not None)
    check("occurred_at не в будущем", r["occurred_at"] <= datetime.now(timezone.utc))
    check("database_name = 'postgres'", r["database_name"] == "postgres",
          f"database_name = {r['database_name']!r}")
    check("user_name заполнен", r["user_name"] not in (None, ""),
          f"user_name = {r['user_name']!r}")
    check("pid_victim > 0", r["pid_victim"] is not None and r["pid_victim"] > 0,
          f"pid_victim = {r['pid_victim']}")
    check("sqlstate = '40P01'", r["sqlstate"] == "40P01",
          f"sqlstate = {r['sqlstate']!r}")
    check("error_message заполнен", r["error_message"] not in (None, ""),
          f"error_message = {r['error_message']!r}")
    check("all_pids содержит >= 2 участников",
          r["all_pids"] is not None and len(r["all_pids"]) >= 2,
          f"all_pids = {r['all_pids']}")
    check("pid_victim входит в all_pids",
          r["pid_victim"] in (r["all_pids"] or []),
          f"pid_victim={r['pid_victim']}, all_pids={r['all_pids']}")
    check("lock_cycle заполнен", r["lock_cycle"] not in (None, ""),
          f"lock_cycle = {r['lock_cycle']!r}")
    check("xid или virtualxid заполнен",
          r["xid"] is not None or r["virtualxid"] is not None,
          f"xid={r['xid']}, virtualxid={r['virtualxid']!r}")

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
    cur.execute("""
        INSERT INTO deadlock_test VALUES
            (1, 'a'), (2, 'b'), (3, 'c'), (4, 'd'), (5, 'e'), (6, 'f')
    """)
    cur.close()
    conn.close()
    print("  Таблица deadlock_test создана (6 строк)")

# =============================================================================
# Сценарий 1: Row-level дедлок
# =============================================================================
def test_row_level():
    print("\n=== СЦЕНАРИЙ 1: Row-level дедлок (2 сессии, 2 строки) ===")
    barrier = threading.Barrier(2)
    started_at = datetime.now(timezone.utc)

    def session_a():
        conn = new_conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE deadlock_test SET val = 'a' WHERE id = 1")
            barrier.wait()
            time.sleep(0.1)
            cur.execute("UPDATE deadlock_test SET val = 'a' WHERE id = 2")
            conn.commit()
        except psycopg2.errors.DeadlockDetected:
            conn.rollback()
        finally:
            conn.close()

    def session_b():
        conn = new_conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE deadlock_test SET val = 'b' WHERE id = 2")
            barrier.wait()
            time.sleep(0.1)
            cur.execute("UPDATE deadlock_test SET val = 'b' WHERE id = 1")
            conn.commit()
        except psycopg2.errors.DeadlockDetected:
            conn.rollback()
        finally:
            conn.close()

    t_a = threading.Thread(target=session_a)
    t_b = threading.Thread(target=session_b)
    t_a.start(); t_b.start()
    t_a.join(timeout=10); t_b.join(timeout=10)

    records = wait_for_records(started_at, 1)
    check("Запись о дедлоке появилась", len(records) >= 1,
          f"Найдено: {len(records)}")

    if records:
        r = records[0]
        check_base_fields(r)
        check("query заполнен", r["query"] not in (None, ""),
              f"query = {r['query']!r}")

# =============================================================================
# Сценарий 2: Table-level lock дедлок
# =============================================================================
def test_table_lock():
    print("\n=== СЦЕНАРИЙ 2: Table-level lock дедлок (LOCK TABLE + FOR UPDATE) ===")

    # Создаём вторую таблицу
    conn = new_conn(autocommit=True)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deadlock_test2 (
            id INTEGER PRIMARY KEY, val TEXT
        )
    """)
    cur.execute("TRUNCATE deadlock_test2")
    cur.execute("INSERT INTO deadlock_test2 VALUES (1, 'x')")
    cur.close()
    conn.close()

    barrier = threading.Barrier(2)
    started_at = datetime.now(timezone.utc)

    def session_a():
        conn = new_conn()
        try:
            cur = conn.cursor()
            # Блокируем строку в deadlock_test
            cur.execute("SELECT * FROM deadlock_test WHERE id = 1 FOR UPDATE")
            barrier.wait()
            time.sleep(0.1)
            # Пытаемся взять эксклюзивный лок на deadlock_test2
            cur.execute("LOCK TABLE deadlock_test2 IN ACCESS EXCLUSIVE MODE")
            conn.commit()
        except psycopg2.errors.DeadlockDetected:
            conn.rollback()
        finally:
            conn.close()

    def session_b():
        conn = new_conn()
        try:
            cur = conn.cursor()
            # Берём эксклюзивный лок на deadlock_test2
            cur.execute("LOCK TABLE deadlock_test2 IN ACCESS EXCLUSIVE MODE")
            barrier.wait()
            time.sleep(0.1)
            # Пытаемся заблокировать строку в deadlock_test — конфликт с A
            cur.execute("SELECT * FROM deadlock_test WHERE id = 1 FOR UPDATE")
            conn.commit()
        except psycopg2.errors.DeadlockDetected:
            conn.rollback()
        finally:
            conn.close()

    t_a = threading.Thread(target=session_a)
    t_b = threading.Thread(target=session_b)
    t_a.start(); t_b.start()
    t_a.join(timeout=10); t_b.join(timeout=10)

    records = wait_for_records(started_at, 1)
    check("Запись о дедлоке появилась", len(records) >= 1,
          f"Найдено: {len(records)}")

    if records:
        r = records[0]
        check_base_fields(r)

# =============================================================================
# Сценарий 3: Advisory locks дедлок
# =============================================================================
def test_advisory_lock():
    print("\n=== СЦЕНАРИЙ 3: Advisory locks дедлок (pg_advisory_xact_lock) ===")

    barrier = threading.Barrier(2)
    started_at = datetime.now(timezone.utc)

    def session_a():
        conn = new_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT pg_advisory_xact_lock(1)")
            barrier.wait()
            time.sleep(0.1)
            cur.execute("SELECT pg_advisory_xact_lock(2)")
            conn.commit()
        except psycopg2.errors.DeadlockDetected as e:
            print(f"  [DEBUG] session_a: DeadlockDetected: {e}")
            conn.rollback()
        except Exception as e:
            print(f"  [DEBUG] session_a: Exception: {type(e).__name__}: {e}")
            conn.rollback()
        finally:
            conn.close()

    def session_b():
        conn = new_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT pg_advisory_xact_lock(2)")
            barrier.wait()
            time.sleep(0.1)
            cur.execute("SELECT pg_advisory_xact_lock(1)")
            conn.commit()
        except psycopg2.errors.DeadlockDetected as e:
            print(f"  [DEBUG] session_b: DeadlockDetected: {e}")
            conn.rollback()
        except Exception as e:
            print(f"  [DEBUG] session_b: Exception: {type(e).__name__}: {e}")
            conn.rollback()
        finally:
            conn.close()

    t_a = threading.Thread(target=session_a)
    t_b = threading.Thread(target=session_b)
    t_a.start(); t_b.start()
    t_a.join(timeout=10); t_b.join(timeout=10)

    records = wait_for_records(started_at, 1)
    check("Запись о дедлоке появилась", len(records) >= 1,
          f"Найдено: {len(records)}")

    if records:
        r = records[0]
        check_base_fields(r)

# =============================================================================
# Сценарий 4: Дедлок из 3 участников (A→B→C→A)
# =============================================================================
def test_three_way():
    print("\n=== СЦЕНАРИЙ 4: Дедлок из 3 участников (A→B→C→A) ===")
    barrier = threading.Barrier(3)
    started_at = datetime.now(timezone.utc)

    def session_a():
        conn = new_conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE deadlock_test SET val = 'a' WHERE id = 1")
            barrier.wait()
            time.sleep(0.1)
            cur.execute("UPDATE deadlock_test SET val = 'a' WHERE id = 2")
            conn.commit()
        except psycopg2.errors.DeadlockDetected:
            conn.rollback()
        finally:
            conn.close()

    def session_b():
        conn = new_conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE deadlock_test SET val = 'b' WHERE id = 2")
            barrier.wait()
            time.sleep(0.1)
            cur.execute("UPDATE deadlock_test SET val = 'b' WHERE id = 3")
            conn.commit()
        except psycopg2.errors.DeadlockDetected:
            conn.rollback()
        finally:
            conn.close()

    def session_c():
        conn = new_conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE deadlock_test SET val = 'c' WHERE id = 3")
            barrier.wait()
            time.sleep(0.1)
            cur.execute("UPDATE deadlock_test SET val = 'c' WHERE id = 1")
            conn.commit()
        except psycopg2.errors.DeadlockDetected:
            conn.rollback()
        finally:
            conn.close()

    t_a = threading.Thread(target=session_a)
    t_b = threading.Thread(target=session_b)
    t_c = threading.Thread(target=session_c)
    t_a.start(); t_b.start(); t_c.start()
    t_a.join(timeout=10); t_b.join(timeout=10); t_c.join(timeout=10)

    records = wait_for_records(started_at, 1)
    check("Запись о дедлоке появилась", len(records) >= 1,
          f"Найдено: {len(records)}")

    if records:
        r = records[0]
        check_base_fields(r)
        check("all_pids содержит >= 3 участников",
              r["all_pids"] is not None and len(r["all_pids"]) >= 3,
              f"all_pids = {r['all_pids']}")

# =============================================================================
# Сценарий 5: store_query = off
# =============================================================================
def test_store_query_off():
    print("\n=== СЦЕНАРИЙ 5: store_query = off (query должен быть NULL) ===")
    set_guc("pg_deadlock_log.store_query", "off")

    barrier = threading.Barrier(2)
    started_at = datetime.now(timezone.utc)

    def session_a():
        conn = new_conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE deadlock_test SET val = 'a' WHERE id = 1")
            barrier.wait()
            time.sleep(0.1)
            cur.execute("UPDATE deadlock_test SET val = 'a' WHERE id = 2")
            conn.commit()
        except psycopg2.errors.DeadlockDetected:
            conn.rollback()
        finally:
            conn.close()

    def session_b():
        conn = new_conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE deadlock_test SET val = 'b' WHERE id = 2")
            barrier.wait()
            time.sleep(0.1)
            cur.execute("UPDATE deadlock_test SET val = 'b' WHERE id = 1")
            conn.commit()
        except psycopg2.errors.DeadlockDetected:
            conn.rollback()
        finally:
            conn.close()

    t_a = threading.Thread(target=session_a)
    t_b = threading.Thread(target=session_b)
    t_a.start(); t_b.start()
    t_a.join(timeout=10); t_b.join(timeout=10)

    records = wait_for_records(started_at, 1)
    check("Запись о дедлоке появилась", len(records) >= 1,
          f"Найдено: {len(records)}")

    if records:
        r = records[0]
        check("query = NULL при store_query=off",
              r["query"] is None or r["query"] == "",
              f"query = {r['query']!r}")
        check("остальные поля заполнены", r["pid_victim"] is not None and r["sqlstate"] == "40P01")

    reset_guc("pg_deadlock_log.store_query")

# =============================================================================
# Сценарий 6: enabled = off
# =============================================================================
def test_enabled_off():
    print("\n=== СЦЕНАРИЙ 6: enabled = off (записей не должно появляться) ===")
    set_guc("pg_deadlock_log.enabled", "off")

    barrier = threading.Barrier(2)
    started_at = datetime.now(timezone.utc)

    def session_a():
        conn = new_conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE deadlock_test SET val = 'a' WHERE id = 1")
            barrier.wait()
            time.sleep(0.1)
            cur.execute("UPDATE deadlock_test SET val = 'a' WHERE id = 2")
            conn.commit()
        except psycopg2.errors.DeadlockDetected:
            conn.rollback()
        finally:
            conn.close()

    def session_b():
        conn = new_conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE deadlock_test SET val = 'b' WHERE id = 2")
            barrier.wait()
            time.sleep(0.1)
            cur.execute("UPDATE deadlock_test SET val = 'b' WHERE id = 1")
            conn.commit()
        except psycopg2.errors.DeadlockDetected:
            conn.rollback()
        finally:
            conn.close()

    t_a = threading.Thread(target=session_a)
    t_b = threading.Thread(target=session_b)
    t_a.start(); t_b.start()
    t_a.join(timeout=10); t_b.join(timeout=10)

    # Ждём немного — записей быть не должно
    time.sleep(5)
    conn = new_conn(autocommit=True)
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM pg_deadlock_log
        WHERE occurred_at >= %s
    """, (started_at,))
    count = cur.fetchone()[0]
    cur.close()
    conn.close()

    check("Записей нет при enabled=off", count == 0,
          f"Найдено записей: {count}, ожидалось: 0")

    reset_guc("pg_deadlock_log.enabled")

# =============================================================================
# Итог
# =============================================================================
def print_summary():
    print("\n=== ИТОГ ===")
    total  = len(results)
    passed = sum(1 for _, ok in results if ok)
    failed = total - passed
    print(f"  Всего:    {total}")
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
    test_row_level()
    test_table_lock()
    test_advisory_lock()
    test_three_way()
    test_store_query_off()
    test_enabled_off()
    ok = print_summary()
    sys.exit(0 if ok else 1)
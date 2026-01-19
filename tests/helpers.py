import os
import psycopg2
import threading

PG_DSN = os.getenv(
    "PG_DEADLOCK_LOG_DSN",
    "dbname=deadlock_test user=ivan"
)
PG_DSN_TCP = os.getenv("PG_DEADLOCK_LOG_DSN_TCP")

def make_conn(dsn = PG_DSN):
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    return conn

def _make_deadlock(conn1, conn2):
    """
    Создаёт дедлок между двумя соединениями и возвращает список исключений.
    """
    cur1 = conn1.cursor()
    cur2 = conn2.cursor()

    # BEGIN в обоих соединениях
    cur1.execute("BEGIN;")
    cur2.execute("BEGIN;")

    # Блокируем разные строки
    cur1.execute("UPDATE t_lock SET val = 'a2' WHERE id = 1;")
    cur2.execute("UPDATE t_lock SET val = 'b2' WHERE id = 2;")

    exc1 = {}
    exc2 = {}

    def t1():
        try:
            cur1.execute("UPDATE t_lock SET val = 'a3' WHERE id = 2;")
        except Exception as e:
            exc1["e"] = e

    def t2():
        try:
            cur2.execute("UPDATE t_lock SET val = 'b3' WHERE id = 1;")
        except Exception as e:
            exc2["e"] = e

    th1 = threading.Thread(target=t1)
    th2 = threading.Thread(target=t2)
    th1.start()
    th2.start()
    th1.join()
    th2.join()

    for conn in (conn1, conn2):
        try:
            conn.rollback()
        except Exception:
            pass

    all_exc = [exc1.get("e"), exc2.get("e")]
    all_exc = [e for e in all_exc if e is not None]
    return all_exc

def _make_deadlock_with_pids(conn1, conn2):
    """
    Создаёт дедлок между двумя соединениями и возвращает:
    - список исключений
    - pid каждого соединения
    - pid соединения, в котором произошёл deadlock (если его можно однозначно определить)
    """
    cur1 = conn1.cursor()
    cur2 = conn2.cursor()

    # Получаем PID для каждого соединения
    cur1.execute("SELECT pg_backend_pid();")
    pid1 = cur1.fetchone()[0]
    cur2.execute("SELECT pg_backend_pid();")
    pid2 = cur2.fetchone()[0]

    # BEGIN в обоих соединениях
    cur1.execute("BEGIN;")
    cur2.execute("BEGIN;")

    # Блокируем разные строки
    cur1.execute("UPDATE t_lock SET val = 'a2' WHERE id = 1;")
    cur2.execute("UPDATE t_lock SET val = 'b2' WHERE id = 2;")

    exc1 = {}
    exc2 = {}

    def t1():
        try:
            cur1.execute("UPDATE t_lock SET val = 'a3' WHERE id = 2;")
        except Exception as e:
            exc1["e"] = e

    def t2():
        try:
            cur2.execute("UPDATE t_lock SET val = 'b3' WHERE id = 1;")
        except Exception as e:
            exc2["e"] = e

    th1 = threading.Thread(target=t1)
    th2 = threading.Thread(target=t2)
    th1.start()
    th2.start()
    th1.join()
    th2.join()

    for conn in (conn1, conn2):
        try:
            conn.rollback()
        except Exception:
            pass

    all_exc = [exc1.get("e"), exc2.get("e")]
    all_exc = [e for e in all_exc if e is not None]

    # Пытаемся определить, в каком соединении был deadlock
    victim_pid = None
    if isinstance(exc1.get("e"), psycopg2.Error) and getattr(exc1["e"], "pgcode", None) == "40P01":
        victim_pid = pid1
    if isinstance(exc2.get("e"), psycopg2.Error) and getattr(exc2["e"], "pgcode", None) == "40P01":
        # если обе жертвы (редко, но аккуратнее не перезатирать)
        if victim_pid is None:
            victim_pid = pid2

    return all_exc, pid1, pid2, victim_pid

def setup_extension(
    conn,
    *,
    enabled: bool = True,
    store_query: bool = True,
    schema: str = "public",
):
    """Общая настройка расширения и GUC-параметров для тестов."""
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_deadlock_log;")
        cur.execute("SET pg_deadlock_log.enabled = %s;", ("on" if enabled else "off",))
        cur.execute(
            "SET pg_deadlock_log.store_query = %s;",
            ("on" if store_query else "off",),
        )
        cur.execute("SET pg_deadlock_log.schema = %s;", (schema,))
    conn.commit()

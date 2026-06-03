import os
import time
import psycopg2
import threading

PG_DSN = os.getenv(
    "PG_DEADLOCK_LOG_DSN",
    "dbname=deadlock_test user=ivan"
)
PG_DSN_TCP = os.getenv("PG_DEADLOCK_LOG_DSN_TCP")


def make_conn(dsn=PG_DSN):
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    return conn


def setup_extension(
    conn,
    *,
    enabled: bool = True,
    store_query: bool = True,
    schema: str = "public",
    worker_timeout: int = 500,
):
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_deadlock_log;")
        cur.execute("SET pg_deadlock_log.enabled = %s;", ("on" if enabled else "off",))
        cur.execute("SET pg_deadlock_log.store_query = %s;", ("on" if store_query else "off",))
        cur.execute("SET pg_deadlock_log.schema = %s;", (schema,))
        cur.execute("SET pg_deadlock_log.worker_timeout = %s;", (worker_timeout,))
    conn.commit()


def wait_for_deadlock_log(
    dsn=PG_DSN,
    schema="public",
    timeout=3.0,
    interval=0.1,
    min_count=1,
):
    """
    Polling: ждём, пока в таблице не появится хотя бы min_count записей.
    Возвращает True, если запись появилась в течение timeout секунд.
    """
    deadline = time.monotonic() + timeout
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        while time.monotonic() < deadline:
            with conn.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM {schema}.pg_deadlock_log;")
                cnt, = cur.fetchone()
            if cnt >= min_count:
                return True
            time.sleep(interval)
        return False
    finally:
        conn.close()


def _make_deadlock(conn1, conn2):
    cur1 = conn1.cursor()
    cur2 = conn2.cursor()

    cur1.execute("BEGIN;")
    cur2.execute("BEGIN;")

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

    return [e for e in (exc1.get("e"), exc2.get("e")) if e is not None]


def _make_deadlock_with_pids(conn1, conn2):
    cur1 = conn1.cursor()
    cur2 = conn2.cursor()

    cur1.execute("SELECT pg_backend_pid();")
    pid1 = cur1.fetchone()[0]
    cur2.execute("SELECT pg_backend_pid();")
    pid2 = cur2.fetchone()[0]

    cur1.execute("BEGIN;")
    cur2.execute("BEGIN;")

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

    all_exc = [e for e in (exc1.get("e"), exc2.get("e")) if e is not None]

    victim_pid = None
    if isinstance(exc1.get("e"), psycopg2.Error) and getattr(exc1["e"], "pgcode", None) == "40P01":
        victim_pid = pid1
    if isinstance(exc2.get("e"), psycopg2.Error) and getattr(exc2["e"], "pgcode", None) == "40P01":
        if victim_pid is None:
            victim_pid = pid2

    return all_exc, pid1, pid2, victim_pid
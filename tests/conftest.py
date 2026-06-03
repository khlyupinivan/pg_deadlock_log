import os
import psycopg2
import pytest

PG_DSN = os.getenv(
    "PG_DEADLOCK_LOG_DSN",
    "dbname=deadlock_test user=ivan"
)


def wait_extension_enabled(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_deadlock_log;")
        cur.execute("SET pg_deadlock_log.enabled = on;")
        cur.execute("SET pg_deadlock_log.store_query = on;")
        cur.execute("SET pg_deadlock_log.schema = 'public';")
        cur.execute("SET pg_deadlock_log.worker_timeout = 500;")
    conn.commit()


@pytest.fixture(scope="session")
def pg_conn():
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = False
    wait_extension_enabled(conn)
    yield conn
    conn.close()


@pytest.fixture
def clean_log(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE public.pg_deadlock_log;")
    pg_conn.commit()
    yield


@pytest.fixture
def setup_lock_table(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS t_lock;")
        cur.execute("CREATE TABLE t_lock(id int PRIMARY KEY, val text);")
        cur.execute("INSERT INTO t_lock VALUES (1, 'a1'), (2, 'b1');")
    pg_conn.commit()
    yield
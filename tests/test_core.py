import psycopg2
import pytest

from tests.helpers import PG_DSN, make_conn, _make_deadlock, setup_extension, wait_for_deadlock_log


def _assert_deadlock(all_exc):
    assert all_exc, "Ожидали хотя бы одну ошибку из-за дедлока"
    assert any(
        isinstance(e, psycopg2.Error) and getattr(e, "pgcode", None) == "40P01"
        for e in all_exc
    ), f"Нет 40P01 среди: {[(type(e), getattr(e, 'pgcode', None)) for e in all_exc]}"


@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_logged_basic():
    """Базовый тест: дедлок логируется, запрос жертвы присутствует в логе."""
    conn1, conn2 = make_conn(), make_conn()
    try:
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=True, schema="public")
        _assert_deadlock(_make_deadlock(conn1, conn2))
    finally:
        for conn in (conn1, conn2):
            try:
                conn.close()
            except Exception:
                pass

    assert wait_for_deadlock_log(PG_DSN), "Запись о дедлоке не появилась в pg_deadlock_log"

    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("""
                SELECT database_name, user_name, pid_victim, application_name,
                       substr(query, 1, 60) AS query_prefix, error_message
                  FROM public.pg_deadlock_log
              ORDER BY id DESC LIMIT 1;
            """)
            row = cur.fetchone()
        assert row is not None
        db_name, user_name, pid_victim, app_name, query_prefix, error_message = row
        assert "deadlock" in (error_message or "").lower()
        q = (query_prefix or "").lower()
        assert (
            "update t_lock set val = 'a3' where id = 2" in q
            or "update t_lock set val = 'b3' where id = 1" in q
        ), f"Неожиданный текст запроса жертвы: {query_prefix!r}"
    finally:
        conn_check.close()


@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_respects_enabled_off():
    """Если enabled=off, дедлок не должен логироваться."""
    conn1, conn2 = make_conn(), make_conn()
    try:
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=False, store_query=True, schema="public")
        _assert_deadlock(_make_deadlock(conn1, conn2))
    finally:
        for conn in (conn1, conn2):
            try:
                conn.close()
            except Exception:
                pass

    found = wait_for_deadlock_log(PG_DSN, timeout=1.0)
    assert not found, "Ожидали 0 записей при enabled=off, но запись появилась"


@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_respects_store_query_off():
    """Если store_query=off, поле query должно быть пустым (NULL или '')."""
    conn1, conn2 = make_conn(), make_conn()
    try:
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=False, schema="public")
        _assert_deadlock(_make_deadlock(conn1, conn2))
    finally:
        for conn in (conn1, conn2):
            try:
                conn.close()
            except Exception:
                pass

    assert wait_for_deadlock_log(PG_DSN), "Запись о дедлоке не появилась в pg_deadlock_log"

    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("""
                SELECT query FROM public.pg_deadlock_log ORDER BY id DESC LIMIT 1;
            """)
            row = cur.fetchone()
        assert row is not None
        query, = row
        assert query in ("", None), f"Ожидали пустой query при store_query=off, получили: {query!r}"
    finally:
        conn_check.close()
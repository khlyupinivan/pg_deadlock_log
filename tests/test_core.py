import time
import psycopg2
import pytest

from tests.helpers import PG_DSN, make_conn, _make_deadlock, setup_extension

@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_logged_basic():
    """
    Базовый тест: дедлок логируется, запрос жертвы присутствует в логе.
    """
    conn1 = make_conn()
    conn2 = make_conn()

    try:
        # Включаем расширение и настройки
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=True, schema="public")

        all_exc = _make_deadlock(conn1, conn2)

        assert all_exc, "Ожидали хотя бы одну ошибку из-за дедлока"
        assert any(
            isinstance(e, psycopg2.Error)
            and getattr(e, "pgcode", None) == "40P01"
            for e in all_exc
        ), f"Нет ошибки deadlock (40P01), были: {[(type(e), getattr(e, 'pgcode', None), str(e)) for e in all_exc]}"
    finally:
        for conn in (conn1, conn2):
            try:
                conn.close()
            except Exception:
                pass

    # Даём hook’у время записать лог
    time.sleep(0.2)

    # Проверяем последнюю запись
    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("""
                SELECT database_name,
                       user_name,
                       pid_victim,
                       application_name,
                       substr(query, 1, 60) AS query_prefix,
                       error_message
                  FROM public.pg_deadlock_log
              ORDER BY id DESC
                 LIMIT 1;
            """)
            row = cur.fetchone()

        assert row is not None, "Запись о дедлоке не найдена в pg_deadlock_log"

        db_name, user_name, pid_victim, app_name, query_prefix, error_message = row

        q = (query_prefix or "").lower()
        err = (error_message or "").lower()

        assert "deadlock" in err

        # допускаем любую из двух участвующих в дедлоке команд
        expected1 = "update t_lock set val = 'a3' where id = 2"
        expected2 = "update t_lock set val = 'b3' where id = 1"
        assert (expected1 in q) or (expected2 in q), (
            f"Ожидали один из запросов жертвы в логе, получили: {query_prefix!r}"
        )
    finally:
        conn_check.close()

@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_respects_enabled_off():
    """
    Если pg_deadlock_log.enabled = off, дедлок не должен логироваться.
    """
    conn1 = make_conn()
    conn2 = make_conn()

    try:
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=False, store_query=True, schema="public")

        all_exc = _make_deadlock(conn1, conn2)

        # дедлок всё равно случается
        assert all_exc, "Ожидали хотя бы одну ошибку из-за дедлока"

        assert any(
            isinstance(e, psycopg2.Error)
            and getattr(e, "pgcode", None) == "40P01"
            for e in all_exc
        ), f"Нет ошибки deadlock (40P01), были: {[(type(e), getattr(e, 'pgcode', None), str(e)) for e in all_exc]}"

    finally:
        for conn in (conn1, conn2):
            try:
                conn.close()
            except Exception:
                pass

    time.sleep(0.2)

    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.pg_deadlock_log;")
            cnt, = cur.fetchone()
        assert cnt == 0, f"Ожидали 0 записей в pg_deadlock_log при enabled=off, получили {cnt}"
    finally:
        conn_check.close()

@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_respects_store_query_off():
    """
    Если pg_deadlock_log.store_query = off, поле query должно быть пустым.
    """
    conn1 = make_conn()
    conn2 = make_conn()

    try:
        # Включаем расширение, выключаем сохранение текста запроса
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=False, schema="public")

        all_exc = _make_deadlock(conn1, conn2)

        assert all_exc, "Ожидали хотя бы одну ошибку из-за дедлока"
        assert any(
            isinstance(e, psycopg2.Error)
            and getattr(e, "pgcode", None) == "40P01"
            for e in all_exc
        ), f"Нет ошибки deadlock (40P01), были: {[(type(e), getattr(e, 'pgcode', None), str(e)) for e in all_exc]}"
    finally:
        for conn in (conn1, conn2):
            try:
                conn.close()
            except Exception:
                pass

    time.sleep(0.2)

    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("""
                SELECT query, error_message
                  FROM public.pg_deadlock_log
              ORDER BY id DESC
                 LIMIT 1;
            """)
            row = cur.fetchone()

        assert row is not None, "Запись о дедлоке не найдена в pg_deadlock_log"
        query, err = row

        # Ожидаем, что query пустой ("" или NULL)
        assert query in ("", None), (
            f"Ожидали пустой query при store_query=off, получили: {query!r}"
        )
    finally:
        conn_check.close()

import psycopg2
import pytest

from .helpers import PG_DSN, make_conn, _make_deadlock, setup_extension

@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_hook_survives_invalid_schema():
    """
    Если pg_deadlock_log.schema указывает на несуществующую или недоступную схему,
    hook не должен ронять backend: соединение должно оставаться живым.
    """
    invalid_schema = "nonexistent_schema_for_pg_deadlock_log"

    conn1 = make_conn()
    conn2 = make_conn()

    try:
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=True, schema=invalid_schema)

        # Пытаемся создать дедлок и удостовериться, что соединения не "обрываются" сервером
        try:
            _ = _make_deadlock(conn1, conn2)
        except psycopg2.OperationalError as e:
            msg = str(e).lower()
            # критично только, если сервер сам закрыл соединение
            if "server closed the connection unexpectedly" in msg:
                pytest.fail(f"Backend упал при некорректной схеме: {e}")
            # иначе это обычная серверная ошибка (например, про схему) — её допускаем
    finally:
        for conn in (conn1, conn2):
            try:
                conn.close()
            except Exception:
                pass

    # В public.pg_deadlock_log при этом записей быть не должно
    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.pg_deadlock_log;")
            cnt, = cur.fetchone()
        assert cnt == 0, (
            f"Ожидали 0 записей в public.pg_deadlock_log при schema={invalid_schema}, получили {cnt}"
        )
    finally:
        conn_check.close()

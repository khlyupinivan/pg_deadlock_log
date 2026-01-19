import time
import psycopg2
import pytest

from .helpers import PG_DSN_TCP, make_conn, _make_deadlock, setup_extension

@pytest.mark.requires_tcp
@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_client_addr_tcp():
    """
    При TCP-подключении client_addr в логе должен быть установлен.

    Использует отдельную переменную окружения PG_DEADLOCK_LOG_DSN_TCP,
    чтобы не ломать остальные тесты.
    """
    print("PG_DSN_TCP in test:", PG_DSN_TCP)
    if not PG_DSN_TCP:
        pytest.skip("PG_DEADLOCK_LOG_DSN_TCP не задан, TCP-тест пропускается")

    conn1 = make_conn(PG_DSN_TCP)
    conn2 = make_conn(PG_DSN_TCP)

    try:
        # Включаем расширение и настройки
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=True, schema="public")

        all_exc = _make_deadlock(conn1, conn2)
        assert all_exc, "Ожидали хотя бы одну ошибку из-за дедлока (TCP)"
    finally:
        for conn in (conn1, conn2):
            try:
                conn.close()
            except Exception:
                pass

    time.sleep(0.2)

    conn_check = psycopg2.connect(PG_DSN_TCP)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("""
                SELECT client_addr, query, error_message
                  FROM public.pg_deadlock_log
              ORDER BY id DESC
                 LIMIT 1;
            """)
            row = cur.fetchone()

        assert row is not None, "Запись о дедлоке не найдена в pg_deadlock_log (TCP)"
        client_addr, query, err = row

        assert client_addr is not None and client_addr != "", (
            f"Ожидали непустой client_addr при TCP-подключении, получили: {client_addr!r}"
        )

        addr_str = str(client_addr)
        # допускаем localhost в IPv4/IPv6 или hostname
        assert any(
            pat in addr_str
            for pat in ("127.0.0.1", "::1", "localhost")
        ), f"Неожиданный client_addr для TCP-подключения: {addr_str!r}"
    finally:
        conn_check.close()

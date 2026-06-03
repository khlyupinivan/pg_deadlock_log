import psycopg2
import pytest

from .helpers import PG_DSN_TCP, make_conn, _make_deadlock, setup_extension, wait_for_deadlock_log


@pytest.mark.requires_tcp
@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_client_addr_tcp():
    """При TCP-подключении client_addr в логе должен быть установлен."""
    if not PG_DSN_TCP:
        pytest.skip("PG_DEADLOCK_LOG_DSN_TCP не задан")

    conn1, conn2 = make_conn(PG_DSN_TCP), make_conn(PG_DSN_TCP)
    try:
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=True, schema="public")
        all_exc = _make_deadlock(conn1, conn2)
        assert all_exc
    finally:
        for conn in (conn1, conn2):
            try:
                conn.close()
            except Exception:
                pass

    assert wait_for_deadlock_log(PG_DSN_TCP), \
        "Запись о дедлоке не появилась в pg_deadlock_log (TCP)"

    conn_check = psycopg2.connect(PG_DSN_TCP)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("""
                SELECT client_addr FROM public.pg_deadlock_log ORDER BY id DESC LIMIT 1;
            """)
            row = cur.fetchone()
        assert row is not None
        client_addr, = row
        assert client_addr not in (None, ""), \
            f"Ожидали непустой client_addr при TCP, получили: {client_addr!r}"
        assert any(
            pat in str(client_addr) for pat in ("127.0.0.1", "::1", "localhost")
        ), f"Неожиданный client_addr: {client_addr!r}"
    finally:
        conn_check.close()
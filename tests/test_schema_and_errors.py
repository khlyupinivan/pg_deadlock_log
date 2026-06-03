import psycopg2
import pytest

from .helpers import PG_DSN, make_conn, _make_deadlock, setup_extension, wait_for_deadlock_log


@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_hook_survives_invalid_schema():
    """
    При несуществующей схеме hook не должен ронять backend.
    В public.pg_deadlock_log записей быть не должно.
    """
    invalid_schema = "nonexistent_schema_for_pg_deadlock_log"
    conn1, conn2 = make_conn(), make_conn()
    try:
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=True, schema=invalid_schema)
        try:
            _make_deadlock(conn1, conn2)
        except psycopg2.OperationalError as e:
            if "server closed the connection unexpectedly" in str(e).lower():
                pytest.fail(f"Backend упал при некорректной схеме: {e}")
    finally:
        for conn in (conn1, conn2):
            try:
                conn.close()
            except Exception:
                pass

    found = wait_for_deadlock_log(PG_DSN, schema="public", timeout=1.0)
    assert not found, "Ожидали 0 записей в public.pg_deadlock_log при невалидной схеме"
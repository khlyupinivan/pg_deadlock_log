import psycopg2
import pytest

from .helpers import (
    PG_DSN, make_conn, _make_deadlock, _make_deadlock_with_pids,
    setup_extension, wait_for_deadlock_log,
)


def _assert_deadlock(all_exc):
    assert all_exc, "Ожидали хотя бы одну ошибку из-за дедлока"
    assert any(
        isinstance(e, psycopg2.Error) and getattr(e, "pgcode", None) == "40P01"
        for e in all_exc
    ), f"Нет 40P01 среди: {[(type(e), getattr(e, 'pgcode', None)) for e in all_exc]}"


@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_custom_schema():
    """Запись должна попадать в указанную схему."""
    schema = "deadlock_log"
    conn_admin = psycopg2.connect(PG_DSN)
    conn_admin.autocommit = True
    try:
        with conn_admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE;")
            cur.execute(f"CREATE SCHEMA {schema};")
            cur.execute(f"""
                CREATE TABLE {schema}.pg_deadlock_log (
                    LIKE public.pg_deadlock_log INCLUDING ALL
                );
            """)
    finally:
        conn_admin.close()

    conn1, conn2 = make_conn(), make_conn()
    try:
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=True, schema=schema)
        _assert_deadlock(_make_deadlock(conn1, conn2))
    finally:
        for conn in (conn1, conn2):
            try:
                conn.close()
            except Exception:
                pass

    assert wait_for_deadlock_log(PG_DSN, schema=schema), \
        f"Запись не появилась в {schema}.pg_deadlock_log"

    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {schema}.pg_deadlock_log;")
            cnt, = cur.fetchone()
        assert cnt >= 1
    finally:
        conn_check.close()


@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_application_name():
    """В лог должна попадать application_name жертвы."""
    app_name = "pg_deadlock_log_test_app"
    conn1, conn2 = make_conn(), make_conn()
    try:
        for i, conn in enumerate((conn1, conn2), start=1):
            setup_extension(conn, enabled=True, store_query=True, schema="public")
            with conn.cursor() as cur:
                cur.execute("SET application_name = %s;", (f"{app_name}_{i}",))
            conn.commit()
        _assert_deadlock(_make_deadlock(conn1, conn2))
    finally:
        for conn in (conn1, conn2):
            try:
                conn.close()
            except Exception:
                pass

    assert wait_for_deadlock_log(PG_DSN)

    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("""
                SELECT application_name FROM public.pg_deadlock_log ORDER BY id DESC LIMIT 1;
            """)
            row = cur.fetchone()
        assert row is not None
        logged_app_name, = row
        assert logged_app_name in (f"{app_name}_1", f"{app_name}_2"), \
            f"Неожиданное application_name: {logged_app_name!r}"
    finally:
        conn_check.close()


@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_db_and_user():
    """В лог должны попадать правильные database_name и user_name."""
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

    assert wait_for_deadlock_log(PG_DSN)

    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("""
                SELECT database_name, user_name FROM public.pg_deadlock_log ORDER BY id DESC LIMIT 1;
            """)
            row = cur.fetchone()
            assert row is not None
            db_name, user_name = row

            cur.execute("SELECT current_database(), current_user;")
            exp_db, exp_user = cur.fetchone()

        assert db_name == exp_db, f"database_name: {db_name!r} != {exp_db!r}"
        assert user_name == exp_user, f"user_name: {user_name!r} != {exp_user!r}"
    finally:
        conn_check.close()


@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_error_message_and_detail():
    """
    error_message должен содержать 'deadlock'.
    error_detail — пустая строка: хук вызывается до генерации ErrorData.
    Граф блокировок логируется в поле lock_cycle.
    """
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

    assert wait_for_deadlock_log(PG_DSN)

    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("""
                SELECT error_message, error_detail, lock_cycle
                  FROM public.pg_deadlock_log
              ORDER BY id DESC LIMIT 1;
            """)
            row = cur.fetchone()
        assert row is not None
        error_message, error_detail, lock_cycle = row

        assert "deadlock" in (error_message or "").lower(), \
            f"Ожидали 'deadlock' в error_message, получили: {error_message!r}"

        assert error_detail in ("", None), \
            f"Ожидали пустой error_detail, получили: {error_detail!r}"

        assert lock_cycle not in (None, ""), "Ожидали непустой lock_cycle"
        assert "waits for" in lock_cycle, \
            f"lock_cycle не содержит 'waits for': {lock_cycle!r}"
    finally:
        conn_check.close()


@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_pid_victim_matches_backend():
    """pid_victim должен совпадать с PID backend'а-жертвы."""
    conn1, conn2 = make_conn(), make_conn()
    try:
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=True, schema="public")
        all_exc, pid1, pid2, victim_pid = _make_deadlock_with_pids(conn1, conn2)
        _assert_deadlock(all_exc)
        assert victim_pid in (pid1, pid2)
    finally:
        for conn in (conn1, conn2):
            try:
                conn.close()
            except Exception:
                pass

    assert wait_for_deadlock_log(PG_DSN)

    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("""
                SELECT pid_victim FROM public.pg_deadlock_log ORDER BY id DESC LIMIT 1;
            """)
            row = cur.fetchone()
        assert row is not None
        logged_pid_victim, = row
        assert logged_pid_victim in (pid1, pid2), \
            f"pid_victim={logged_pid_victim}, pid'ы были {pid1} и {pid2}"
        if victim_pid is not None:
            assert logged_pid_victim == victim_pid, \
                f"Ожидали pid_victim={victim_pid}, в логе {logged_pid_victim}"
    finally:
        conn_check.close()


@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_sqlstate():
    """sqlstate должен быть '40P01'."""
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

    assert wait_for_deadlock_log(PG_DSN)

    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("""
                SELECT sqlstate, error_message FROM public.pg_deadlock_log ORDER BY id DESC LIMIT 1;
            """)
            row = cur.fetchone()
        assert row is not None
        sqlstate, error_message = row
        assert sqlstate == "40P01", f"Ожидали '40P01', получили {sqlstate!r}"
        assert "deadlock" in (error_message or "").lower()
    finally:
        conn_check.close()


@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_search_path_captured():
    """В лог должен попадать search_path жертвы."""
    conn1, conn2 = make_conn(), make_conn()
    try:
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=True, schema="public")
            with conn.cursor() as cur:
                cur.execute("SET search_path = myschema, public;")
            conn.commit()
        _assert_deadlock(_make_deadlock(conn1, conn2))
    finally:
        for conn in (conn1, conn2):
            try:
                conn.close()
            except Exception:
                pass

    assert wait_for_deadlock_log(PG_DSN)

    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("""
                SELECT search_path FROM public.pg_deadlock_log ORDER BY id DESC LIMIT 1;
            """)
            row = cur.fetchone()
        assert row is not None
        logged_sp, = row
        assert logged_sp is not None, "search_path в логе пустой"
        assert "myschema" in logged_sp, \
            f"Ожидали 'myschema' в search_path, получили: {logged_sp!r}"
    finally:
        conn_check.close()


@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_search_path_with_user():
    """В лог должен попадать search_path с $user."""
    conn1, conn2 = make_conn(), make_conn()
    try:
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=True, schema="public")
            with conn.cursor() as cur:
                cur.execute("CREATE SCHEMA IF NOT EXISTS myschema;")
                cur.execute('SET search_path = "$user", public, myschema;')
            conn.commit()
        _assert_deadlock(_make_deadlock(conn1, conn2))
    finally:
        for conn in (conn1, conn2):
            try:
                conn.close()
            except Exception:
                pass

    assert wait_for_deadlock_log(PG_DSN)

    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("""
                SELECT search_path FROM public.pg_deadlock_log ORDER BY id DESC LIMIT 1;
            """)
            row = cur.fetchone()
        assert row is not None
        logged_sp, = row
        assert "$user" in logged_sp or "public" in logged_sp, \
            f"search_path выглядит странно: {logged_sp!r}"
    finally:
        conn_check.close()


@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_search_path_with_quotes():
    """В лог должен попадать search_path со спецсимволами."""
    conn1, conn2 = make_conn(), make_conn()
    try:
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=True, schema="public")
            with conn.cursor() as cur:
                cur.execute('CREATE SCHEMA IF NOT EXISTS "My.Schema";')
                cur.execute('SET search_path = "My.Schema", public;')
            conn.commit()
        _assert_deadlock(_make_deadlock(conn1, conn2))
    finally:
        for conn in (conn1, conn2):
            try:
                conn.close()
            except Exception:
                pass

    assert wait_for_deadlock_log(PG_DSN)

    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("""
                SELECT search_path FROM public.pg_deadlock_log ORDER BY id DESC LIMIT 1;
            """)
            row = cur.fetchone()
        assert row is not None
        logged_sp, = row
        assert "My.Schema" in logged_sp, \
            f"Ожидали 'My.Schema' в search_path, получили: {logged_sp!r}"
    finally:
        conn_check.close()


@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_xid_and_virtualxid_present():
    """xid и virtualxid жертвы должны попадать в лог."""
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

    assert wait_for_deadlock_log(PG_DSN)

    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("""
                SELECT xid, virtualxid FROM public.pg_deadlock_log ORDER BY id DESC LIMIT 1;
            """)
            row = cur.fetchone()
        assert row is not None
        xid, virtualxid = row
        if xid is not None:
            assert isinstance(xid, int), f"xid не int: {xid!r}"
        assert virtualxid not in (None, ""), \
            f"Ожидали непустой virtualxid, получили: {virtualxid!r}"
        assert "/" in virtualxid, \
            f"Ожидали формат 'procNumber/lxid', получили: {virtualxid!r}"
    finally:
        conn_check.close()


@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_all_pids_and_lock_cycle():
    """
    all_pids должен содержать PID обоих участников дедлока.
    lock_cycle должен содержать граф в формате 'X waits for Y'.
    """
    conn1, conn2 = make_conn(), make_conn()
    try:
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=True, schema="public")
        all_exc, pid1, pid2, _ = _make_deadlock_with_pids(conn1, conn2)
        _assert_deadlock(all_exc)
    finally:
        for conn in (conn1, conn2):
            try:
                conn.close()
            except Exception:
                pass

    assert wait_for_deadlock_log(PG_DSN)

    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("""
                SELECT all_pids, lock_cycle FROM public.pg_deadlock_log ORDER BY id DESC LIMIT 1;
            """)
            row = cur.fetchone()
        assert row is not None
        all_pids, lock_cycle = row

        assert all_pids is not None and len(all_pids) >= 2, \
            f"Ожидали >= 2 PID в all_pids, получили: {all_pids!r}"
        assert pid1 in all_pids, f"pid1={pid1} не найден в all_pids={all_pids}"
        assert pid2 in all_pids, f"pid2={pid2} не найден в all_pids={all_pids}"

        assert lock_cycle not in (None, ""), "Ожидали непустой lock_cycle"
        assert "waits for" in lock_cycle, \
            f"lock_cycle не содержит 'waits for': {lock_cycle!r}"
        assert str(pid1) in lock_cycle, f"pid1={pid1} не в lock_cycle: {lock_cycle!r}"
        assert str(pid2) in lock_cycle, f"pid2={pid2} не в lock_cycle: {lock_cycle!r}"
    finally:
        conn_check.close()
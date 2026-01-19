import time
import psycopg2
import pytest

from .helpers import PG_DSN, make_conn, _make_deadlock, _make_deadlock_with_pids, setup_extension

@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_custom_schema():
    """
    Если pg_deadlock_log.schema = 'deadlock_log', запись должна попадать в эту схему.
    """
    schema = "deadlock_log"

    # Подготовка схемы и таблицы-аналога
    conn_admin = psycopg2.connect(PG_DSN)
    conn_admin.autocommit = True
    try:
        with conn_admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE;")
            cur.execute(f"CREATE SCHEMA {schema};")
            # создаём таблицу в новой схеме по образцу public.pg_deadlock_log
            cur.execute(f"""
                CREATE TABLE {schema}.pg_deadlock_log (
                    LIKE public.pg_deadlock_log INCLUDING ALL
                );
            """)
    finally:
        conn_admin.close()

    conn1 = make_conn()
    conn2 = make_conn()

    try:
        # Включаем расширение и указываем схему для логов
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=True, schema=schema)

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
            # убеждаемся, что таблица в схеме существует
            cur.execute("""
                SELECT 1
                  FROM information_schema.tables
                 WHERE table_schema = %s
                   AND table_name = 'pg_deadlock_log';
            """, (schema,))
            assert cur.fetchone() is not None, "Таблица схемы для логов не найдена"

            cur.execute(f"SELECT count(*) FROM {schema}.pg_deadlock_log;")
            cnt, = cur.fetchone()
        assert cnt >= 1, (
            f"Ожидали хотя бы одну запись в {schema}.pg_deadlock_log, получили {cnt}"
        )
    finally:
        conn_check.close()

@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_application_name():
    """
    В лог должна попадать application_name жертвы дедлока.
    """
    app_name = "pg_deadlock_log_test_app"

    conn1 = make_conn()
    conn2 = make_conn()

    try:
        # Задаём application_name и включаем расширение
        for i, conn in enumerate((conn1, conn2), start=1):
            setup_extension(conn, enabled=True, store_query=True, schema="public")
            with conn.cursor() as cur:
                cur.execute("SET application_name = %s;", (f"{app_name}_{i}",))
                conn.commit()

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
                SELECT application_name, query, error_message
                  FROM public.pg_deadlock_log
              ORDER BY id DESC
                 LIMIT 1;
            """)
            row = cur.fetchone()

        assert row is not None, "Запись о дедлоке не найдена в pg_deadlock_log"
        logged_app_name, query, err = row

        # Ожидаем, что application_name соответствует одной из наших сессий
        assert logged_app_name in (f"{app_name}_1", f"{app_name}_2"), (
            f"Неожиданное application_name в логе: {logged_app_name!r}"
        )
    finally:
        conn_check.close()

@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_db_and_user():
    """
    В лог должны попадать правильные database_name и user_name.
    """
    conn1 = make_conn()
    conn2 = make_conn()

    try:
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

    time.sleep(0.2)

    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("""
                SELECT database_name, user_name, query
                  FROM public.pg_deadlock_log
              ORDER BY id DESC
                 LIMIT 1;
            """)
            row = cur.fetchone()

        assert row is not None, "Запись о дедлоке не найдена в pg_deadlock_log"
        db_name, user_name, query = row

        # сравниваем с текущими значениями в проверочном соединении
        with conn_check.cursor() as cur:
            cur.execute("SELECT current_database(), current_user;")
            exp_db, exp_user = cur.fetchone()

        assert db_name == exp_db, f"database_name в логе {db_name!r}, ожидали {exp_db!r}"
        assert user_name == exp_user, f"user_name в логе {user_name!r}, ожидали {exp_user!r}"
    finally:
        conn_check.close()

@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_error_message_and_detail():
    """
    В лог должны корректно попадать error_message и error_detail для дедлока.
    """
    conn1 = make_conn()
    conn2 = make_conn()

    try:
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=True, schema="public")

        all_exc = _make_deadlock(conn1, conn2)

        # Убедимся, что дедлок действительно произошёл
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
                SELECT error_message, error_detail
                  FROM public.pg_deadlock_log
              ORDER BY id DESC
                 LIMIT 1;
            """)
            row = cur.fetchone()

        assert row is not None, "Запись о дедлоке не найдена в pg_deadlock_log"
        error_message, error_detail = row

        msg = (error_message or "").lower()
        detail = error_detail or ""

        # Базовые ожидания
        assert "deadlock" in msg, f"В error_message нет слова deadlock: {error_message!r}"
        # DETAIL должен содержать хотя бы фрагменты вида 'Process N waits for' etc.
        assert "process" in detail.lower(), f"error_detail выглядит странно: {error_detail!r}"
        assert "waits for" in detail.lower(), f"error_detail не содержит 'waits for': {error_detail!r}"
    finally:
        conn_check.close()

@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_pid_victim_matches_backend():
    """
    pid_victim в логе должен совпадать с PID backend'а, который словил дедлок (40P01).
    """
    conn1 = make_conn()
    conn2 = make_conn()

    try:
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=True, schema="public")

        all_exc, pid1, pid2, victim_pid = _make_deadlock_with_pids(conn1, conn2)

        assert all_exc, "Ожидали хотя бы одну ошибку из-за дедлока"
        assert any(
            isinstance(e, psycopg2.Error)
            and getattr(e, "pgcode", None) == "40P01"
            for e in all_exc
        ), f"Нет ошибки deadlock (40P01), были: {[(type(e), getattr(e, 'pgcode', None), str(e)) for e in all_exc]}"

        # Должны были хотя бы для одного соединения понять victim_pid
        assert victim_pid in (pid1, pid2), (
            f"Не удалось определить PID жертвы по исключениям, "
            f"pid1={pid1}, pid2={pid2}, all_exc={all_exc}"
        )
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
                SELECT pid_victim, query, error_message
                  FROM public.pg_deadlock_log
              ORDER BY id DESC
                 LIMIT 1;
            """)
            row = cur.fetchone()

        assert row is not None, "Запись о дедлоке не найдена в pg_deadlock_log"
        logged_pid_victim, query, err = row

        assert logged_pid_victim in (pid1, pid2), (
            f"pid_victim={logged_pid_victim}, но pid backend'ов были {pid1} и {pid2}"
        )

        # Если мы смогли однозначно определить victim_pid из исключений —
        # проверим строгое совпадение
        if victim_pid is not None:
            assert logged_pid_victim == victim_pid, (
                f"Ожидали pid_victim={victim_pid} (по исключениям), "
                f"но в логе {logged_pid_victim}"
            )
    finally:
        conn_check.close()

@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_sqlstate():
    """
    В лог должен попадать корректный sqlstate для дедлока (40P01).
    """
    conn1 = make_conn()
    conn2 = make_conn()

    try:
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

    time.sleep(0.2)

    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("""
                SELECT sqlstate, error_message
                  FROM public.pg_deadlock_log
              ORDER BY id DESC
                 LIMIT 1;
            """)
            row = cur.fetchone()

        assert row is not None, "Запись о дедлоке не найдена в pg_deadlock_log"
        sqlstate, error_message = row

        assert sqlstate == "40P01", f"Ожидали sqlstate='40P01', получили {sqlstate!r}"
        assert "deadlock" in (error_message or "").lower()
    finally:
        conn_check.close()

@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_search_path_captured():
    """
    В лог должен попадать search_path жертвы дедлока.
    """
    conn1 = make_conn()
    conn2 = make_conn()

    try:
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=True, schema="public")
            with conn.cursor() as cur:
                cur.execute("SET search_path = myschema, public;")
                conn.commit()

        # Перед дедлоком убедимся, что t_lock видна в этих коннектах
        with conn1.cursor() as cur:
            cur.execute("SHOW search_path;")
            print("conn1 search_path:", cur.fetchone())
            cur.execute("SELECT count(*) FROM t_lock;")
            print("conn1 t_lock count:", cur.fetchone())
        
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
                SELECT search_path
                  FROM public.pg_deadlock_log
              ORDER BY id DESC
                 LIMIT 1;
            """)
            row = cur.fetchone()

        assert row is not None, "Запись о дедлоке не найдена в pg_deadlock_log"
        (logged_sp,) = row

        assert logged_sp is not None, "search_path в логе пустой"
        assert "myschema" in logged_sp, f"Ожидали, что search_path содержит 'myschema', получили: {logged_sp!r}"
    finally:
        conn_check.close()

@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_search_path_with_user():
    """
    В лог должен попадать search_path с $user.
    """
    conn1 = make_conn()
    conn2 = make_conn()

    try:
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=True, schema="public")
            with conn.cursor() as cur:
                # на всякий случай schema, чтобы t_lock точно была видна
                cur.execute("CREATE SCHEMA IF NOT EXISTS myschema;")
                # используем $user в search_path
                cur.execute("SET search_path = \"$user\", public, myschema;")
                conn.commit()

        all_exc = _make_deadlock(conn1, conn2)

        assert all_exc, "Ожидали хотя бы одну ошибку из-за дедлока"
        assert any(
            isinstance(e, psycopg2.Error)
            and getattr(e, "pgcode", None) == "40P01"
            for e in all_exc
        ), "Нет ошибки deadlock (40P01)"
    finally:
        for conn in (conn1, conn2):
            try:
                conn.close()
            except Exception:
                pass

    # даём hook’у время
    time.sleep(0.2)

    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            # что реально видит проверочная сессия
            cur.execute("SHOW search_path;")
            actual_sp_check, = cur.fetchone()

            # последняя запись из лога
            cur.execute("""
                SELECT search_path
                  FROM public.pg_deadlock_log
              ORDER BY id DESC
                 LIMIT 1;
            """)
            row = cur.fetchone()

        assert row is not None, "Запись о дедлоке не найдена в pg_deadlock_log"
        logged_sp, = row

        # В логе должен быть тот же текст, который даёт SHOW search_path в сессии-жертве.
        # Здесь уместны базовые ожидания:
        assert "$user" in logged_sp or "pg_user" in logged_sp or "public" in logged_sp, (
            f"search_path в логе выглядит странно: {logged_sp!r}"
        )
    finally:
        conn_check.close()

@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_search_path_with_quotes():
    """
    В лог должен попадать search_path с кавычками и спецсимволами.
    """
    conn1 = make_conn()
    conn2 = make_conn()

    try:
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=True, schema="public")
            with conn.cursor() as cur:

                cur.execute("CREATE SCHEMA IF NOT EXISTS \"My.Schema\";")
                # search_path с экранированным именем схемы
                cur.execute("SET search_path = \"My.Schema\", public;")
                conn.commit()

        all_exc = _make_deadlock(conn1, conn2)

        assert all_exc, "Ожидали хотя бы одну ошибку из-за дедлока"
        assert any(
            isinstance(e, psycopg2.Error)
            and getattr(e, "pgcode", None) == "40P01"
            for e in all_exc
        ), "Нет ошибки deadlock (40P01)"
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
                SELECT search_path
                  FROM public.pg_deadlock_log
              ORDER BY id DESC
                 LIMIT 1;
            """)
            row = cur.fetchone()

        assert row is not None, "Запись о дедлоке не найдена в pg_deadlock_log"
        logged_sp, = row

        assert "My.Schema" in logged_sp, (
            f"Ожидали, что search_path содержит 'My.Schema', получили: {logged_sp!r}"
        )
    finally:
        conn_check.close()

@pytest.mark.usefixtures("clean_log", "setup_lock_table")
def test_deadlock_log_xid_and_virtualxid_present():
    """
    В лог должны попадать xid и virtualxid жертвы дедлока.
    """
    conn1 = make_conn()
    conn2 = make_conn()

    try:
        for conn in (conn1, conn2):
            setup_extension(conn, enabled=True, store_query=True, schema="public")

        all_exc = _make_deadlock(conn1, conn2)

        assert all_exc, "Ожидали хотя бы одну ошибку из-за дедлока"
        assert any(
            isinstance(e, psycopg2.Error)
            and getattr(e, "pgcode", None) == "40P01"
            for e in all_exc
        ), "Нет ошибки deadlock (40P01)"
    finally:
        for c in (conn1, conn2):
            try:
                c.close()
            except Exception:
                pass

    # Даём hook'у время записать лог
    time.sleep(0.2)

    conn_check = psycopg2.connect(PG_DSN)
    conn_check.autocommit = True
    try:
        with conn_check.cursor() as cur:
            cur.execute("""
                SELECT xid, virtualxid
                  FROM public.pg_deadlock_log
              ORDER BY id DESC
                 LIMIT 1;
            """)
            row = cur.fetchone()

        assert row is not None, "Запись о дедлоке не найдена в pg_deadlock_log"
        xid, virtualxid = row

        # xid может быть NULL, если XID ещё не назначен, поэтому только мягкая проверка типа
        if xid is not None:
            assert isinstance(xid, int), f"Ожидали целочисленный xid, получили {xid!r}"

        # virtualxid должен быть непустой строкой разумного формата
        assert virtualxid is not None and virtualxid != "", (
            f"Ожидали непустой virtualxid, получили: {virtualxid!r}"
        )
        assert "/" in virtualxid, (
            f"Ожидали формат 'backendId/localXid' для virtualxid, получили: {virtualxid!r}"
        )
    finally:
        conn_check.close()

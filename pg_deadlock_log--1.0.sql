-- pg_deadlock_log--1.0.sql
-- SQL-часть расширения pg_deadlock_log
-- Выполняется при CREATE EXTENSION pg_deadlock_log;

CREATE TABLE pg_deadlock_log (
    id              bigserial PRIMARY KEY,
    occurred_at     timestamptz    NOT NULL DEFAULT now(),  -- время когда зафиксирован дедлок

    database_name   name           NOT NULL,                -- имя базы данных, где случился дедлок
    user_name       name           NOT NULL,                -- роль жертвы
    pid_victim      integer        NOT NULL,                -- PID процесса-жертвы

    sqlstate        text           NOT NULL,                -- код ошибки (для deadlock'а '40P01')
    
    query           text,                                   -- текст запроса жертвы
    error_message   text           NOT NULL,                -- основное сообщение об ошибке
    error_detail    text,                                   -- detail/контекст, если удастся вытащить

    application_name text,                                  -- application_name жертвы, если доступен
    client_addr      text,                                  -- адрес клиента
    search_path      text,                                   -- search_path жертвы

    xid              bigint,                                -- текущий XID жертвы
    virtualxid       text                                   -- virtual XID жертвы
);
# Расширение pg_deadlock_log
pg_deadlock_log — расширение PostgreSQL, которое перехватывает дедлоки и записывает подробную информацию о них в служебную таблицу. Это позволяет анализировать и расследовать взаимные блокировки постфактум.

Архитектура основана на deadlock_log_hook (вызывается из DeadLockCheck() в момент обнаружения дедлока), разделяемой памяти (shared memory) и Background Worker (BGWorker), который подхватывает запись из shm и делает INSERT через SPI. Это исключает прямое использование SPI в backend'е в момент дедлока и делает расширение значительно устойчивее.

## Возможности
1.	Логирование каждой ошибки дедлока в таблицу pg_deadlock_log.
2.	Сбор ключевого контекста:
    1.	база данных и пользователь;
    2.	PID процесса-жертвы;
    3.	SQLSTATE и текст ошибки;
    4.	запрос-жертва (опционально);
    5.	application_name, адрес клиента;
    6.	search_path;
    7.	текущий xid и virtualxid жертвы;
	8.  all_pids; 
	9.  lock_cycle в формате "X waits for Y; Y waits for Z".
3.	Минимальное вмешательство в остальной код:
    1.	работает через deadlock_log_hook;
    2.	аккуратно вызывает прежний хук, если он был.

## Установка
### 1.	Сборка и установка
В каталоге с исходниками:
```bash
make && sudo make install $$ sudo service postgresql restart
```

Требования:
1.	PostgreSQL, собранный с поддержкой модулей C;
2.	стандартные dev пакеты:
    1.	postgresql-server-dev-* на Debian/Ubuntu;
    2.	postgresqlXX-devel на RHEL подобных системах.

### 2.	Подключение в базе данных
В нужной БД:
```sql
CREATE EXTENSION pg_deadlock_log;
```
По умолчанию таблица pg_deadlock_log создаётся в схеме public. Это можно переопределить через GUC параметр (см. ниже).

# Таблица pg_deadlock_log
DDL (упрощённо):

```sql
CREATE TABLE pg_deadlock_log ( 
id bigserial PRIMARY KEY, 
occurred_at      timestamptz NOT NULL DEFAULT now(),  -- время фиксации дедлока
database_name    name        NOT NULL,                -- имя БД
user_name        name        NOT NULL,                -- роль жертвы
pid_victim       integer     NOT NULL,                -- PID процесса-жертвы

sqlstate         text        NOT NULL,                -- код ошибки (обычно '40P01')

query            text,                                -- запрос жертвы (опционально)
error_message    text        NOT NULL,                -- основное сообщение об ошибке
error_detail     text,                                -- DETAIL/контекст, если есть

application_name text,                                -- application_name жертвы
client_addr      text,                                -- адрес клиента
search_path      text,                                -- search_path жертвы

xid              bigint,                              -- текущий XID жертвы (если есть)
virtualxid       text,                                -- virtual XID жертвы, формат "backendId/lxid"

all_pids         integer[]    NOT NULL DEFAULT '{}',  -- PID всех участников дедлока
lock_cycle       text                                 -- граф блокировок: "X waits for Y; Y waits for Z"
);
```

Замечания:
1.	error_message всегда 'deadlock detected'.
2.	virtualxid всегда должен быть непустым для backend’а, участвующего в дедлоке (этот момент покрыт тестами).
3.	Поле query заполняется только при включённом параметре pg_deadlock_log.store_query.

# Настройки (GUC параметры)

Расширение определяет четыре GUC параметра.

## pg_deadlock_log.enabled (boolean)

Включает или выключает логирование дедлоков.

Тип: bool 

Уровень: SUSET (может менять суперпользователь) 

Значение по умолчанию: on

Примеры:
```sql
SET pg_deadlock_log.enabled = off; ALTER SYSTEM SET pg_deadlock_log.enabled = on; SELECT pg_reload_conf();
```

## pg_deadlock_log.store_query (boolean)

Управляет логированием текста запроса жертвы.

Тип: bool 

Уровень: SUSET 

Значение по умолчанию: on

При on модуль использует глобальную переменную debug_query_string, чтобы получить текст текущего запроса и сохранить его в поле query.

Примеры:
```sql
SET pg_deadlock_log.store_query = off; -- не писать запросы
```
## pg_deadlock_log.schema (text)

Схема, в которой ищется таблица pg_deadlock_log.

Тип: text 

Уровень: SUSET 

Значение по умолчанию: 'public'

Важно: параметр влияет только на то, в какую схему модуль будет делать INSERT. Саму таблицу нужно создать там же (через CREATE EXTENSION или вручную).

Пример:
```sql
ALTER SYSTEM SET pg_deadlock_log.schema = 'monitoring'; SELECT pg_reload_conf();
-- затем пересоздать/переместить таблицу: 
CREATE SCHEMA IF NOT EXISTS monitoring; ALTER TABLE public.pg_deadlock_log SET SCHEMA monitoring;
```

## pg_deadlock_log.worker_timeout (int)
Таймаут ожидания латча в BGWorker

Тип: int

Уровень: SUSET

Значение по умолчанию: 50000

# Как это работает внутри

## Хук на deadlock_log_hook
При загрузке модуля (_PG_init):
1.	расширение регистрирует свой pg_deadlock_log_hook в deadlock_log_hook;
2.	запоминает предыдущий хук (если был), чтобы вызывать его первым

При выгрузке (_PG_fini) хук откатывается обратно.

## Сбор контекста дедлока
Контекст логически разделён на части, каждая реализована в своём модуле.
1.	fill_from_deadlockinfo:
	1.	victim_pid;
	2.	occurred_at, sqlstate = "40P01";
	3.	client_addr.
2.	fill_participants:
	1. 	info->all_procs[].
3.	fill_lock_cycle:
	1.	all_procs[].
4.	fill_tx_info:
	1.	schema;
	2.	search_path;
	3.  xid, virtualxid;
	4.  db_name, user_name, app_name;
	5.  application_name.


## Передача через shm.
pg_deadlock_log_write_shm захватывает LWLock, копирует данные в фиксированные буферы DeadlockLogShm, выставляет pending = true. Если предыдущая запись не обработана — перезаписывается.

## BGWorker. 
Подключается к БД postgres, в цикле ждёт на  WaitLatch, при pending == true копирует snaphot локально, сбрасывает флаг и выполняет INSERT через SPI в отдельной транзакции. Ошибки логирует как WARNING.

## Производительность и безопасное использование
1.	Запись осуществляется только при возникновении дедлоков — то есть редко в нормальных системах.
2.	Тем не менее, вставка идёт через SPI и запускает свои внутренние транзакции, что вносит небольшой overhead в момент дедлока.
3.	Вся логика обёрнута в PG_TRY/PG_CATCH и не должна ломать основной поток обработки ошибок.

## Рекомендации:
1.	Не использовать в проде без тестирования под своей нагрузкой.
2.	При большом количестве дедлоков (что уже само по себе аномалия) следите за ростом таблицы pg_deadlock_log и настраивайте ротацию/архивацию.

# Тестирование
В репозитории предусмотрен набор pytest тестов, которые:
1.	искусственно создают дедлок между двумя соединениями;
2.	проверяют:
	1.	что в таблице появляется запись с sqlstate = '40P01';
	2.	что корректно заполняются pid_victim, database_name, user_name;
	3.	что, при наличии транзакции, логируются xid и virtualxid;
	4.	что учитываются настройки pg_deadlock_log.enabled и pg_deadlock_log.store_query.

Скрипт для запуска:
```
./run_tests.sh
```
или напрямую:
```
pytest
```

# Ограничения и дальнейшие улучшения
Ограничения:
1.	Буфер shm — одна запись: при высокой частоте дедлоков промежуточные записи теряются.
2.	BGWorker подключается только к БД postgres.

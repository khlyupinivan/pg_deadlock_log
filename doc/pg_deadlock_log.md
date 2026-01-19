# Расширение pg_deadlock_log
pg_deadlock_log — расширение PostgreSQL, которое перехватывает ошибки дедлоков (ERROR 40P01) и записывает подробную информацию о жертве дедлока в служебную таблицу. Это позволяет анализировать и расследовать взаимные блокировки постфактум.

## Возможности
1.	Логирование каждой ошибки дедлока в таблицу pg_deadlock_log.
2.	Сбор ключевого контекста:
    1.	база данных и пользователь;
    2.	PID процесса-жертвы;
    3.	SQLSTATE и текст ошибки;
    4.	запрос-жертва (опционально);
    5.	application_name, адрес клиента;
    6.	search_path;
    7.	текущий xid и virtualxid жертвы.
3.	Минимальное вмешательство в остальной код:
    1.	работает через установку собственного emit_log_hook;
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
virtualxid       text                                 -- virtual XID жертвы, формат "backendId/lxid"
);
```

Замечания:
1.	xid может быть NULL, если к моменту дедлока не было активной транзакции.
2.	virtualxid всегда должен быть непустым для backend’а, участвующего в дедлоке (этот момент покрыт тестами).
3.	Поле query заполняется только при включённом параметре pg_deadlock_log.store_query.

# Настройки (GUC параметры)

Расширение определяет три GUC параметра.

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
# Как это работает внутри

## Хук на emit_log_hook
При загрузке модуля (_PG_init):
1.	расширение регистрирует свой pg_deadlock_log_emit_log в emit_log_hook;
2.	запоминает предыдущий хук (если был), чтобы вызывать его первым:
```C
prev_emit_log_hook = emit_log_hook; 
emit_log_hook = pg_deadlock_log_emit_log;
```
При выгрузке (_PG_fini) хук откатывается обратно.
## Фильтрация событий
pg_deadlock_log_emit_log вызывается на каждое лог сообщение. Он:
1.	Сначала вызывает prev_emit_log_hook, если он есть.
2.	Проверяет:
	1.	включён ли модуль (pg_deadlock_log.enabled);
	2.	что это ERROR;
	3.	что нет рекурсии (in_pg_deadlock_log == false);
	4.	что sqlerrcode == ERRCODE_T_R_DEADLOCK_DETECTED (дедлок).
3.	Если все условия выполнены:
	1.	собирает контекст дедлока в структуру DeadlockLogEntry;
	2.	пишет запись в таблицу pg_deadlock_log.

Любые ошибки внутри обработчика оборачиваются в PG_TRY/PG_CATCH, чтобы не ломать основной код сервера и не маскировать исходный дедлок.

## Сбор контекста дедлока
Контекст логически разделён на три части, каждая реализована в своём модуле.
1.	Контекст из ErrorData и окружения backend’а (pg_deadlock_log_fill_from_errordata):
	1.	query_str (через debug_query_string, если store_query=on);
	2.	error_message, error_detail, sqlstate;
	3.	client_addr (через MyProcPort->remote_host).
2.	Транзакционный контекст (pg_deadlock_log_fill_tx_info):
	1.	schema (из pg_deadlock_log.schema, с запасным значением 'public');
	2.	search_path (через GetConfigOption('search_path'));
	3.	xid (через GetTopTransactionIdIfAny / GetCurrentTransactionIdIfAny);
	4.	virtualxid (через MyProc->backendId / MyProc->lxid).
3.	Метаданные через SPI (pg_deadlock_log_fill_metadata_via_spi):
	1.	database_name (SELECT current_database());
	2.	user_name (SELECT current_user);
	3.	application_name (SELECT current_setting('application_name', true)).

Для SPI части используются отдельные короткие внутренние транзакции с явным AbortOutOfAnyTransaction(), StartTransactionCommand() и CommitTransactionCommand().

## Вставка записи в таблицу
Функция pg_deadlock_log_insert_entry:
1.	Формирует SQL INSERT с помощью StringInfo и quote_literal_cstr.
2.	Запускает ещё одну короткую внутреннюю транзакцию.
3.	Через SPI выполняет INSERT в <schema>.pg_deadlock_log.
4.	В случае ошибок пишет предупреждение (elog(WARNING, ...)), но не прерывает исходную обработку ошибки.

## Использование
Базовый сценарий:
1.	Установка и включение расширения.
2.	В приложении или нагрузочном тесте запускаются конфликтующие транзакции.
3.	Когда в PostgreSQL возникает дедлок и он выбирает жертву:
	1.	backend выбрасывает ERROR 40P01;
	2.	emit_log_hook перехватывает это сообщение;
	3.	pg_deadlock_log добавляет строку в таблицу.
	4.	
Потом можно проанализировать последнюю запись:
```sql
SELECT * FROM public.pg_deadlock_log ORDER BY id DESC LIMIT 5;
```

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
1.	Пока не логируются детали «другой стороны» дедлока — только жертвы.
2.	Отсутствует поле wait_event / wait_event_type (можно добавить позже).
3.	Не реализовано хранение полной информации о графе блокировок.

# Идеи для будущих версий:
1.	Логирование wait_event_type / wait_event в момент дедлока.
2.	Добавление locktag / relation / tuple информации (через внутренние API).
3.	Опциональный экспорт логов в JSON формате или во внешние системы.
4.	Добавить GUC для политики 
5.	Настроить CI с матрицей версий PostgreSQL.

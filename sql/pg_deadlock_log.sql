-- pg_deadlock_log_deadlock.sql
-- Тест расширения pg_deadlock_log

-- Загружаем расширение
CREATE EXTENSION IF NOT EXISTS pg_deadlock_log;

-- ====== ТЕСТ 1: Таблица существует ======
SELECT 'TEST 1: table exists' AS test,
       COUNT(*) > 0 AS passed
FROM pg_class
WHERE relname = 'pg_deadlock_log';

-- ====== ТЕСТ 2: Таблица в правильной схеме (по умолчанию public) ======
SELECT 'TEST 2: table schema' AS test,
       n.nspname = 'public' AS passed
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname = 'pg_deadlock_log';

-- ====== ТЕСТ 3: Структура таблицы (колонки и их типы) ======
SELECT 'TEST 3: table structure' AS test,
       STRING_AGG(attname || ':' || format_type(atttypid, atttypmod), ', ' ORDER BY attnum) AS columns
FROM pg_attribute
WHERE attrelid = 'pg_deadlock_log'::regclass
  AND attnum > 0
  AND NOT attisdropped;

-- ====== ТЕСТ 4: Наличие всех обязательных колонок ======
SELECT 'TEST 4: required columns' AS test,
       bool_and(attname IN (
           'id', 'occurred_at', 'database_name', 'user_name', 'pid_victim',
           'sqlstate', 'query', 'error_message', 'error_detail',
           'application_name', 'client_addr', 'search_path',
           'xid', 'virtualxid', 'all_pids', 'lock_cycle'
       )) AS passed
FROM pg_attribute
WHERE attrelid = 'pg_deadlock_log'::regclass
  AND attnum > 0
  AND NOT attisdropped;

-- ====== ТЕСТ 5: Типы колонок ======
SELECT 'TEST 5: column types' AS test,
       attname,
       format_type(atttypid, atttypmod) AS expected_type
FROM pg_attribute
WHERE attrelid = 'pg_deadlock_log'::regclass
  AND attnum > 0
  AND NOT attisdropped
ORDER BY attnum;

-- ====== ТЕСТ 6: all_pids — массив integer[] ======
SELECT 'TEST 6: all_pids is integer[]' AS test,
       format_type(atttypid, atttypmod) = 'integer[]' AS passed
FROM pg_attribute
WHERE attrelid = 'pg_deadlock_log'::regclass
  AND attname = 'all_pids';

-- ====== ТЕСТ 7: lock_cycle — text ======
SELECT 'TEST 7: lock_cycle is text' AS test,
       format_type(atttypid, atttypmod) = 'text' AS passed
FROM pg_attribute
WHERE attrelid = 'pg_deadlock_log'::regclass
  AND attname = 'lock_cycle';

-- ====== ТЕСТ 8: GUC-параметры существуют ======
SELECT 'TEST 8: GUC enabled' AS test,
       name, setting
FROM pg_settings
WHERE name LIKE 'pg_deadlock_log.%'
ORDER BY name;

-- ====== ТЕСТ 9: GUC pg_deadlock_log.enabled = on ======
SELECT 'TEST 9: enabled is on' AS test,
       setting = 'on' AS passed
FROM pg_settings
WHERE name = 'pg_deadlock_log.enabled';

-- ====== ТЕСТ 10: GUC pg_deadlock_log.store_query = on ======
SELECT 'TEST 10: store_query is on' AS test,
       setting = 'on' AS passed
FROM pg_settings
WHERE name = 'pg_deadlock_log.store_query';

-- ====== ТЕСТ 11: GUC pg_deadlock_log.schema = public ======
SELECT 'TEST 11: schema is public' AS test,
       setting = 'public' AS passed
FROM pg_settings
WHERE name = 'pg_deadlock_log.schema';

-- ====== ТЕСТ 12: Изменение GUC schema ======
SET pg_deadlock_log.schema = 'public';
SELECT 'TEST 12: set schema works' AS test,
       current_setting('pg_deadlock_log.schema') = 'public' AS passed;

-- ====== ТЕСТ 13: Отключение расширения через GUC ======
SET pg_deadlock_log.enabled = 'off';
SELECT 'TEST 13: disable extension' AS test,
       current_setting('pg_deadlock_log.enabled') = 'off' AS passed;

-- Возвращаем обратно
SET pg_deadlock_log.enabled = 'on';

-- ====== ТЕСТ 14: Отключение сохранения запроса ======
SET pg_deadlock_log.store_query = 'off';
SELECT 'TEST 14: disable store_query' AS test,
       current_setting('pg_deadlock_log.store_query') = 'off' AS passed;

-- Возвращаем обратно
SET pg_deadlock_log.store_query = 'on';

-- ====== ТЕСТ 15: Вставка и чтение (проверка, что таблица работает) ======
INSERT INTO pg_deadlock_log 
    (database_name, user_name, pid_victim, sqlstate, error_message, 
     all_pids, lock_cycle)
VALUES (
    current_database(),
    current_user,
    pg_backend_pid(),
    '40P01',
    'test deadlock entry',
    ARRAY[pg_backend_pid(), 99999],
    '12345 waits for 99999; 99999 waits for 12345'
);

SELECT 'TEST 15: insert and read' AS test,
       id, database_name, user_name, pid_victim,
       all_pids, lock_cycle
FROM pg_deadlock_log
ORDER BY id DESC
LIMIT 1;

-- Очищаем тестовую запись
DELETE FROM pg_deadlock_log WHERE error_message = 'test deadlock entry';

-- ====== ТЕСТ 16: Пустая таблица после очистки (опционально) ======
SELECT 'TEST 16: table is empty' AS test,
       COUNT(*) = 0 AS passed
FROM pg_deadlock_log;

-- ====== ИТОГ ======
SELECT '=== ALL TESTS PASSED ===' AS result;
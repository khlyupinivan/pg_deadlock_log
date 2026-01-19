-- Загружаем расширение
CREATE EXTENSION pg_deadlock_log;

-- Проверяем, что таблица существует и в нужной схеме
SELECT n.nspname AS schema_name,
       c.relname AS table_name
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname = 'pg_deadlock_log'
ORDER BY 1, 2;

-- Проверяем структуру (минимально: имена колонок и типы)
SELECT attnum,
       attname,
       format_type(atttypid, atttypmod) AS type
FROM pg_attribute
WHERE attrelid = 'pg_deadlock_log'::regclass
  AND attnum > 0
  AND NOT attisdropped
ORDER BY attnum;

-- Проверяем GUC-параметры
SHOW pg_deadlock_log.enabled;
SHOW pg_deadlock_log.store_query;
SHOW pg_deadlock_log.schema;

-- Меняем schema в сессии и проверяем, что GUC меняется
SET pg_deadlock_log.schema = 'public';
SHOW pg_deadlock_log.schema;
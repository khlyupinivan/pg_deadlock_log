-- Загружаем наше расширение
CREATE EXTENSION IF NOT EXISTS pg_deadlock_log;

-- На всякий случай очищаем лог
TRUNCATE TABLE pg_deadlock_log;

-- Проверяем, что dblink доступен (для теста двух соединений)
CREATE EXTENSION IF NOT EXISTS dblink;

-- Создаём таблицу для дедлока
DROP TABLE IF EXISTS t_lock;
CREATE TABLE t_lock(id int PRIMARY KEY, val text);

INSERT INTO t_lock VALUES (1, 'a1'), (2, 'b1');

-- Открываем второе соединение через dblink к этой же базе
-- Имя подключения: 'con2'
SELECT dblink_connect('con2', current_setting('listen_addresses') || ' ' || current_database());

-- В реальности, для локального тестового кластера достаточно:
-- SELECT dblink_connect('con2', 'dbname=' || current_database());

-- Начинаем транзакцию в текущем сеансе
BEGIN;

-- В текущем сеансе блокируем строку id = 1
UPDATE t_lock SET val = 'a2' WHERE id = 1;

-- В параллельном сеансе через dblink блокируем строку id = 2
SELECT dblink_exec('con2', 'BEGIN');
SELECT dblink_exec('con2', 'UPDATE t_lock SET val = ''b2'' WHERE id = 2');

-- Теперь создаём дедлок:
-- 1) текущий сеанс пытается обновить id = 2, но он заблокирован во втором
DO $$
BEGIN
    BEGIN
        UPDATE t_lock SET val = 'a3' WHERE id = 2;
    EXCEPTION WHEN deadlock_detected THEN
        -- гасим исключение, оно нам только для генерации события
        RAISE NOTICE 'deadlock detected in main session';
    END;
END$$;

-- 2) второй сеанс пытается обновить id = 1 — но это уже не обязательно,
--    Postgres детектирует дедлок, когда один из процессов упирается в уже заблокированный ресурс.

-- Завершаем транзакцию во втором сеансе и закрываем соединение
SELECT dblink_exec('con2', 'ROLLBACK');
SELECT dblink_disconnect('con2');

-- Закрываем нашу основную транзакцию (должна быть уже в aborted state после ERROR/NOTICE)
ROLLBACK;

-- Дадим бэкэнду шанс дописать запись (на всякий случай)
SELECT pg_sleep(0.1);

-- Смотрим, что в логе ровно ОДНА запись о дедлоке, и она соответствует нашей базе/пользователю.
SELECT database_name,
       user_name,
       pid_victim,
       application_name,
       query,
       error_message
FROM pg_deadlock_log
ORDER BY id DESC
LIMIT 1;
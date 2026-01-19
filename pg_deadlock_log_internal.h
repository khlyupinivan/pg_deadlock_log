#ifndef PG_DEADLOCK_LOG_INTERNAL_H
#define PG_DEADLOCK_LOG_INTERNAL_H

#include "postgres.h"
#include "utils/elog.h"

/*
 * Структура с данными о дедлоке, из которых собирается строка
 * в pg_deadlock_log.
 */
typedef struct DeadlockLogEntry
{
    /* из ErrorData / окружения */
    const char *query_str;
    const char *error_msg;
    const char *error_detail;
    const char *sqlstate_str;
    const char *app_name;
    const char *client_addr_str;

    /* tx-контекст */
    const char *schema;
    const char *search_path_str;
    char *xid_str;
    char *vxid_str;

    /* метаданные через SPI */
    char *db_name;
    char *user_name;
} DeadlockLogEntry;

/* GUC-переменные (определены в pg_deadlock_log.c) */
extern bool pg_deadlock_log_enabled;
extern bool pg_deadlock_log_store_query;
extern char *pg_deadlock_log_schema;

/* Заполнение DeadlockLogEntry */
extern void pg_deadlock_log_fill_from_errordata(ErrorData *edata,
                                                DeadlockLogEntry *entry);
extern void pg_deadlock_log_fill_tx_info(DeadlockLogEntry *entry);
extern bool pg_deadlock_log_fill_metadata_via_spi(DeadlockLogEntry *entry);

/* Выполнение вставки */
extern void pg_deadlock_log_insert_entry(const DeadlockLogEntry *entry);

#endif /* PG_DEADLOCK_LOG_INTERNAL_H */
#ifndef PG_DEADLOCK_LOG_INTERNAL_H
#define PG_DEADLOCK_LOG_INTERNAL_H

#include "postgres.h"
#include "storage/lock.h"
#include "storage/latch.h"
#include "storage/lwlock.h"
#include "utils/elog.h"

#define DEADLOCK_MAX_QUERY       1024
#define DEADLOCK_MAX_MSG         512
#define DEADLOCK_MAX_DETAIL      512
#define DEADLOCK_MAX_SQLSTATE    16
#define DEADLOCK_MAX_APPNAME     64
#define DEADLOCK_MAX_ADDR        64
#define DEADLOCK_MAX_DBNAME      64
#define DEADLOCK_MAX_USERNAME    64
#define DEADLOCK_MAX_SCHEMA      64
#define DEADLOCK_MAX_SEARCH_PATH 256
#define DEADLOCK_MAX_XID         32
#define DEADLOCK_MAX_VXID        32
#define DEADLOCK_MAX_LOCK_CYCLE  1024
#define DEADLOCK_MAX_PIDS        64


typedef struct DeadlockLogShm
{
    LWLock  lock;
    bool    pending;
    Latch  *worker_latch;

    int     victim_pid;
    int     all_pids[DEADLOCK_MAX_PIDS];
    int     n_all_pids;

    char    lock_cycle[DEADLOCK_MAX_LOCK_CYCLE];
    char    query_str[DEADLOCK_MAX_QUERY];
    char    error_msg[DEADLOCK_MAX_MSG];
    char    sqlstate_str[DEADLOCK_MAX_SQLSTATE];
    char    app_name[DEADLOCK_MAX_APPNAME];
    char    client_addr_str[DEADLOCK_MAX_ADDR];
    char    db_name[DEADLOCK_MAX_DBNAME];
    char    user_name[DEADLOCK_MAX_USERNAME];
    char    schema[DEADLOCK_MAX_SCHEMA];
    char    search_path_str[DEADLOCK_MAX_SEARCH_PATH];
    char    xid_str[DEADLOCK_MAX_XID];
    char    vxid_str[DEADLOCK_MAX_VXID];
} DeadlockLogShm;


/*
 * Структура с данными о дедлоке, из которых собирается строка
 * в pg_deadlock_log. Заполняется из DeadlockInfo + ErrorData
 */
typedef struct DeadlockLogEntry
{
    /* Из DeadlockInfo */
    int          victim_pid;            /* PID жертвы */
    TimestampTz  occurred_at;           /* время снятия слепка DB*/

    int         *all_pids;              /* массив PID всех участников */
    int          n_all_pids;            /* количество участников */

    char        *lock_cycle;             /* текст: "X waits for Y; Y waits for Z" */

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
extern int pg_deadlock_log_worker_timeout;

extern DeadlockLogShm *pg_deadlock_shm;

/* Заполнение из DeadlockInfo */
extern void pg_deadlock_log_fill_from_deadlockinfo(const DeadlockInfo *info,
                                                   DeadlockLogEntry *entry);

extern void pg_deadlock_log_fill_participants(const DeadlockInfo *info,
                                              DeadlockLogEntry *entry);

/* Построение текстового описания графа блокировок */
extern void pg_deadlock_log_fill_lock_cycle(const DeadlockInfo *info,
                                            DeadlockLogEntry *entry); 
                                               
/* Заполнение DeadlockLogEntry */
extern void pg_deadlock_log_fill_tx_info(DeadlockLogEntry *entry);
extern bool pg_deadlock_log_fill_metadata_via_spi(DeadlockLogEntry *entry);

extern void pg_deadlock_log_fill_participant_queries(const DeadlockInfo *info,
                                                      DeadlockLogEntry *entry);

extern void pg_deadlock_log_write_shm(const DeadlockLogEntry *entry);
PGDLLEXPORT void pg_deadlock_log_worker_main(Datum main_arg);

#endif /* PG_DEADLOCK_LOG_INTERNAL_H */
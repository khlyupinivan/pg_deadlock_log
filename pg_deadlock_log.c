#include "postgres.h"
#include "fmgr.h"

#include "access/xact.h"
#include "executor/spi.h"
#include "libpq/libpq-be.h"
#include "miscadmin.h"
#include "postmaster/bgworker.h"
#include "storage/ipc.h"
#include "storage/lock.h"
#include "storage/lwlock.h"
#include "storage/shmem.h"
#include "tcop/tcopprot.h"
#include "utils/builtins.h"
#include "utils/errcodes.h"
#include "utils/elog.h"
#include "utils/guc.h"

#include "pg_deadlock_log_internal.h"

PG_MODULE_MAGIC;


/* GUC-параметры — определяем здесь, объявляем в header */
bool pg_deadlock_log_enabled = true;
bool pg_deadlock_log_store_query = true;
char *pg_deadlock_log_schema = "public";
int pg_deadlock_log_worker_timeout = 50000;

int deadlock_log_retention_days;
int deadlock_log_max_records;

/* Предыдущий deadlock-хук для цепочки вызовов */
DeadlockLogShm *pg_deadlock_shm = NULL;
static deadlock_log_hook_type prev_deadlock_hook = NULL;

/* Флаг защиты от рекурсии */
static bool in_deadlock_hook = false;

/* Прототипы обязательных функций */
void _PG_init(void);
void _PG_fini(void);
PG_FUNCTION_INFO_V1(pg_deadlock_log_vacuum);

static void pg_deadlock_log_hook(const DeadlockInfo *info);
static void pg_deadlock_log_shmem_request(void);
static void pg_deadlock_log_shmem_startup(void);

static shmem_request_hook_type  prev_shmem_request_hook  = NULL;
static shmem_startup_hook_type  prev_shmem_startup_hook  = NULL;

static void
pg_deadlock_log_shmem_request(void)
{
    if (prev_shmem_request_hook)
        prev_shmem_request_hook();
    RequestAddinShmemSpace(sizeof(DeadlockLogShm));
    RequestNamedLWLockTranche("pg_deadlock_log", 1);
}

static void
pg_deadlock_log_shmem_startup(void)
{
    bool found;

    if (prev_shmem_startup_hook)
        prev_shmem_startup_hook();

    LWLockAcquire(AddinShmemInitLock, LW_EXCLUSIVE);

    pg_deadlock_shm = ShmemInitStruct("pg_deadlock_log",
                                      sizeof(DeadlockLogShm),
                                      &found);
    if (!found)
    {
        MemSet(pg_deadlock_shm, 0, sizeof(DeadlockLogShm));
        pg_deadlock_shm->pending = false;
    }

    LWLockRelease(AddinShmemInitLock);
}

void _PG_init(void)
{
    DefineCustomBoolVariable("pg_deadlock_log.enabled",
                             "Enable logging deadlocks into pg_deadlock_log table.",
                             NULL,
                             &pg_deadlock_log_enabled,
                             true,
                             PGC_SUSET,
                             0,
                             NULL, NULL, NULL);

    DefineCustomBoolVariable("pg_deadlock_log.store_query",
                             "Store victim query text in pg_deadlock_log.",
                             NULL,
                             &pg_deadlock_log_store_query,
                             true,
                             PGC_SUSET,
                             0,
                             NULL, NULL, NULL);

    DefineCustomStringVariable("pg_deadlock_log.schema",
                               "Schema for pg_deadlock_log table.",
                               NULL,
                               &pg_deadlock_log_schema,
                               "public",
                               PGC_SUSET,
                               0,
                               NULL, NULL, NULL);

    DefineCustomIntVariable("pg_deadlock_log.worker_timeout",
                            "BGWorker latch timeout in milliseconds.",
                            NULL,
                            &pg_deadlock_log_worker_timeout,
                            50000,   /* default: 50s */
                            100,     /* min: 100ms */
                            300000,  /* max: 5min */
                            PGC_SUSET,
                            0,
                            NULL, NULL, NULL);

    DefineCustomIntVariable("pg_deadlock_log.retention_days",
                            "Delete deadlock log entries older than this many days.",
                            NULL,
                            &deadlock_log_retention_days,
                            30,    /* default */
                            1,     /* min */
                            3650,  /* max — 10 лет */
                            PGC_SIGHUP,
                            0,
                            NULL, NULL, NULL);

    DefineCustomIntVariable("pg_deadlock_log.max_records",
                            "Maximum number of deadlock log entries to keep.",
                            NULL,
                            &deadlock_log_max_records,
                            1000,       /* default */
                            1,          /* min */
                            1000000,    /* max */
                            PGC_SIGHUP,
                            0,
                            NULL, NULL, NULL);

    /* Shared memory */
    prev_shmem_request_hook  = shmem_request_hook;
    shmem_request_hook       = pg_deadlock_log_shmem_request;

    prev_shmem_startup_hook  = shmem_startup_hook;
    shmem_startup_hook       = pg_deadlock_log_shmem_startup;

    /* Deadlock hook */
    prev_deadlock_hook = deadlock_log_hook;
    deadlock_log_hook  = pg_deadlock_log_hook;

    /* Регистрируем BGWorker */
    {
        BackgroundWorker worker;
        MemSet(&worker, 0, sizeof(worker));
        snprintf(worker.bgw_name,          BGW_MAXLEN, "pg_deadlock_log worker");
        snprintf(worker.bgw_type,          BGW_MAXLEN, "pg_deadlock_log");
        snprintf(worker.bgw_library_name,  BGW_MAXLEN, "pg_deadlock_log");
        snprintf(worker.bgw_function_name, BGW_MAXLEN, "pg_deadlock_log_worker_main");
        worker.bgw_flags       = BGWORKER_SHMEM_ACCESS |
                                 BGWORKER_BACKEND_DATABASE_CONNECTION;
        worker.bgw_start_time  = BgWorkerStart_RecoveryFinished;
        worker.bgw_restart_time = 1;
        worker.bgw_main_arg    = (Datum) 0;
        RegisterBackgroundWorker(&worker);
    }

}

void _PG_fini(void)
{
    deadlock_log_hook   = prev_deadlock_hook;
    shmem_startup_hook  = prev_shmem_startup_hook;
    shmem_request_hook  = prev_shmem_request_hook;
}

/*
 * deadlock_log_hook: вызывается из DeadLockCheck() в момент,
 * когда дедлок обнаружен, жертва выбрана, но ещё не убита.
 *
 * В этом хуке у нас есть доступ к:
 *   - info->victim_proc   (PGPROC жертвы)
 *   - info->all_procs[]   (все участники цикла)
 *   - info->cycle_edges[] (ребра графа)
 *   - info->n_procs, info->n_cycle_edges
 */
static void pg_deadlock_log_hook(const DeadlockInfo *info)
{
    DeadlockLogEntry entry;

    /* Сначала передаём управление предыдущему хуку в цепочке */
    if (prev_deadlock_hook)
        prev_deadlock_hook(info);

    if (!pg_deadlock_log_enabled)
        return;

    /* Защита от рекурсии */
    if (in_deadlock_hook)
        return;

    if (info == NULL)
        return;

    in_deadlock_hook = true;

    PG_TRY();
    {
        MemSet(&entry, 0, sizeof(DeadlockLogEntry));

        /* 1. Заполняем из DeadlockInfo*/
        pg_deadlock_log_fill_from_deadlockinfo(info, &entry);
        pg_deadlock_log_fill_participants(info, &entry);
        pg_deadlock_log_fill_lock_cycle(info, &entry);
        pg_deadlock_log_fill_tx_info(&entry);

        if (pg_deadlock_log_store_query && debug_query_string != NULL)
            entry.query_str = debug_query_string;

        if (entry.db_name == NULL && MyProcPort != NULL)
            entry.db_name = MyProcPort->database_name;

        if (entry.user_name == NULL && MyProcPort != NULL)
            entry.user_name = MyProcPort->user_name;

        if (entry.app_name == NULL)
            entry.app_name = application_name;

        /* Пишем в shared memory — BGWorker подхватит */
        pg_deadlock_log_write_shm(&entry);

        if (entry.all_pids)   pfree(entry.all_pids);
        if (entry.lock_cycle) pfree(entry.lock_cycle);
        if (entry.xid_str)    pfree(entry.xid_str);
        if (entry.vxid_str)   pfree(entry.vxid_str);
    }
    PG_CATCH();
    {
        ErrorData *edata;

        if (entry.all_pids)   pfree(entry.all_pids);
        if (entry.lock_cycle) pfree(entry.lock_cycle);
        if (entry.xid_str)    pfree(entry.xid_str);
        if (entry.vxid_str)   pfree(entry.vxid_str);

        edata = CopyErrorData();
        FlushErrorState();
        elog(LOG, "pg_deadlock_log: error in hook: %s",
             edata->message ? edata->message : "<null>");
        FreeErrorData(edata);
    }
    PG_END_TRY();

    in_deadlock_hook = false;
}

Datum
pg_deadlock_log_vacuum(PG_FUNCTION_ARGS)
{
    int         deleted_total = 0;
    int         deleted;
    Oid         argtypes[1];
    Datum       values[1];

    SPI_connect();

    /* Шаг 1: удалить записи старше retention_days */
    argtypes[0] = INT4OID;
    values[0]   = Int32GetDatum(deadlock_log_retention_days);

    SPI_execute_with_args(
        "DELETE FROM pg_deadlock_log "
        "WHERE occurred_at < now() - ($1 * interval '1 day')",
        1, argtypes, values, NULL, false, 0);

    deleted = SPI_processed;
    deleted_total += deleted;

    elog(LOG, "pg_deadlock_log_vacuum: deleted %d old records (older than %d days)",
         deleted, deadlock_log_retention_days);

    /* Шаг 2: удалить лишние записи сверх max_records */
    argtypes[0] = INT4OID;
    values[0]   = Int32GetDatum(deadlock_log_max_records);

    SPI_execute_with_args(
        "DELETE FROM pg_deadlock_log "
        "WHERE id IN ("
        "    SELECT id FROM pg_deadlock_log "
        "    ORDER BY occurred_at DESC "
        "    OFFSET $1"
        ")",
        1, argtypes, values, NULL, false, 0);

    deleted = SPI_processed;
    deleted_total += deleted;

    elog(LOG, "pg_deadlock_log_vacuum: deleted %d excess records (max_records=%d)",
         deleted, deadlock_log_max_records);

    SPI_finish();

    PG_RETURN_INT32(deleted_total);
}